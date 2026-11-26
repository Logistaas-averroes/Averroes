/**
 * static/app.js
 *
 * Logistaas Ads Intelligence — SPA frontend logic.
 * PR-ADS-025B — Dashboard Live Data Upgrade
 * PR-ADS-094 — Production UX Stabilization (hash routing, window truth, help drawer fix)
 *
 * Rules:
 *  - No hardcoded secrets.
 *  - No external tracking or analytics.
 *  - No third-party JS dependencies.
 *  - Auth state managed via HTTP-only session cookie (not accessible to JS).
 *  - Role-based UI: admin sees run triggers + health page; viewer/mdr read-only.
 *  - SPA routing via hash (#/page-name) with show/hide DOM switching.
 */

"use strict";

// ── Constants ──────────────────────────────────────────────────────────────

// PR-ADS-153C: "opportunities" (In Progress Leads) is retired as a page; its
// old URL resolves to a redirect rather than a dead route (PR-ADS-178B: to the
// Pipeline page's leads list, filtered to open_working).
// PR-ADS-153D: "waste" (Flagged Waste Terms) is retired the same way — the page
// and its loader are deleted, and the key survives ONLY so #/waste resolves to
// the redirect below instead of 404-ing an existing bookmark.
// PR-ADS-177B: "roas-campaigns" (Campaign Revenue & Spend) is deleted; it is
// one page with Campaign Evidence under "campaigns" and survives only as a
// redirect below.
// PR-ADS-178B: "leads" and "deals" are one page, "pipeline"; they and the
// older "opportunities" key survive only as redirects below.
// PR-ADS-177C: "keywords" (Keyword Evidence) is one page with Search Terms
// under "search-terms" — "Keywords & Search Terms" (owner decision 3,
// 2026-11-24) — and survives only as a redirect to its Keywords tab.
const PAGES = ["dashboard", "campaigns", "waste", "search-terms", "ngrams", "pipeline", "gclid-attribution", "scheduler", "health", "backfill", "roas-countries", "revenue-health", "channels", "system"];

// Retired pages that must still resolve. Each maps to its canonical destination
// and (optionally) the filter intent the old page expressed (PR-ADS-153C §26).
const RETIRED_PAGE_REDIRECTS = {
  // In Progress Leads == the open_working operational filter on the leads
  // list (PR-ADS-178B: on the Pipeline page, opened at its leads section).
  opportunities: { page: "pipeline", section: "leads", status: "open_working" },
  // PR-ADS-178B: Leads and Closed-Won Deals are one page, Pipeline. Neither
  // key is in PAGES; each survives only here and carries its intent — the
  // section of the Pipeline page the old page was.
  leads: { page: "pipeline", section: "leads" },
  deals: { page: "pipeline", section: "deals" },
  // PR-ADS-153D: Flagged Waste Terms == the Flagged view of Search Terms.
  // The old page is deleted, not hidden — the redirect carries its INTENT
  // (land on the flagged terms), which is what an old bookmark actually wanted.
  waste: { page: "search-terms", tab: "flagged" },
  // PR-ADS-168B: Revenue by Source is deleted; Channels carries closed-won
  // deals, customers and revenue per channel (PR-ADS-168A). The key is NOT in
  // PAGES — this entry is the only place it survives, so an old bookmark
  // lands on Channels instead of the Dashboard fallback.
  "revenue-by-source": { page: "channels" },
  // PR-ADS-176B: the Countries evidence page is deleted; Countries is one page
  // (the mart's country view, business windows, strict spend gate). Same rule
  // as the Revenue by Source key: it is NOT in PAGES and survives only here, so
  // an old #/geo bookmark lands on Countries instead of the Dashboard.
  geo: { page: "roas-countries" },
  // PR-ADS-177B: Campaign Revenue & Spend is deleted; Campaigns is one page
  // (the mart's settled campaign view, business windows, strict spend gate).
  // Same rule: NOT in PAGES, it survives only here, so an old
  // #/roas-campaigns bookmark lands on Campaigns instead of the Dashboard.
  "roas-campaigns": { page: "campaigns" },
  // PR-ADS-177C: Keyword Evidence is the Keywords tab of the one Keywords &
  // Search Terms page (owner decision 3, 2026-11-24). NOT in PAGES; an old
  // #/keywords bookmark lands on that tab.
  keywords: { page: "search-terms", tab: "keywords" },
};

// PR-ADS-174A: the six-item menu. Every page id in PAGES belongs to exactly one
// item (a retired key through its redirect); an item opens its first page the
// viewer may see, and an item with more than one page shows them as the tab
// strip at the top of the page (#section-tabs-bar). Admin gating is NOT decided
// here: showApp() hides the admin-only tabs exactly as it hid their sidebar
// entries, and applyNavGating() reads those tabs back.
const NAV_ITEMS = [
  { id: "overview", label: "Overview", pages: ["dashboard"] },
  { id: "channels", label: "Channels", pages: ["channels"] },
  // PR-ADS-177B: one Campaigns page. PR-ADS-177C: one Keywords & Search
  // Terms page (owner decision 3, 2026-11-24).
  { id: "campaigns", label: "Campaigns", pages: ["campaigns", "search-terms"] },
  // PR-ADS-176B: one page, so no tab strip.
  { id: "countries", label: "Countries", pages: ["roas-countries"] },
  // PR-ADS-178B: one Pipeline page (leads and deals), so no tab strip.
  { id: "pipeline", label: "Pipeline", pages: ["pipeline"] },
  { id: "system", label: "System", pages: ["system", "scheduler", "health", "revenue-health", "backfill", "gclid-attribution"] },
];

// The menu item a page belongs to. A retired key or a hash alias is resolved
// to the page it lands on first, so the highlighted item is the destination's.
function navItemForPage(page) {
  const retired = RETIRED_PAGE_REDIRECTS[page];
  const canonical = retired ? retired.page : (HASH_ROUTE_ALIASES[page] || page);
  const item = NAV_ITEMS.find((it) => it.pages.includes(canonical));
  return item ? item.id : null;
}

// PR-ADS-110/113: Revenue & Attribution pages. These use business-revenue
// windows (not ad-style 7d/14d/30d/60d) and a clean readiness model — the global
// ad-window bar, monitoring warning, help/explanation blocks, and any
// attribution-audit controls are suppressed here. Deals (Closed-Won Revenue
// Ledger) and Revenue Health (admin diagnostics) joined in PR-ADS-113.
// PR-ADS-134: the Dashboard is now the Executive Overview — a business-truth
// command center on business windows — so it joins the clean revenue chrome.
// PR-ADS-153C: Leads is a CRM/business page — business windows, clean chrome.
// PR-ADS-162A: Channels has its own acquisition-window selector — clean chrome.
// PR-ADS-162C: System is a plain status page — clean chrome.
// PR-ADS-168B review round 1: the one label on every link from a
// business-window Dashboard figure to Channels (services/canonical_contract.
// CHANNELS_LINK_LABEL). Channels does not reproduce those figures.
const CHANNELS_LINK_LABEL = "Open Channels (different windows and grouping)";

// PR-ADS-178B: "pipeline" replaces "leads" and "deals".
const REVENUE_PAGES = ["dashboard", "campaigns", "roas-countries", "pipeline", "revenue-health", "channels", "system"];

function isRevenuePage(pageId) {
  return REVENUE_PAGES.includes(pageId);
}

// PR-ADS-124: one shared chrome guard for the global ad-window bar, the page
// help button, and the monitoring warning banner. Revenue & Attribution pages
// are clean business-decision pages — none of that ad UI clutter belongs there.
// Called from every route lifecycle point (showApp, navigate start + after
// _currentPage is set, hashchange, and after the freshness/monitoring loaders)
// so an in-flight async paint can never leak chrome back onto a revenue page.
// A body.is-revenue-page class provides a CSS failsafe even if JS misses a spot.
function applyPageChrome(page) {
  const revenuePage = isRevenuePage(page);

  const timeRangeBar = document.getElementById("time-range-bar");
  if (timeRangeBar) timeRangeBar.hidden = revenuePage;

  const helpBtn = document.getElementById("page-help-btn");
  if (helpBtn && revenuePage) helpBtn.hidden = true;

  const monitoringBanner = document.getElementById("monitoring-banner");
  if (monitoringBanner && revenuePage) monitoringBanner.hidden = true;

  // PR-ADS-134: the Executive Overview carries its own truth footer — the
  // global freshness strip is diagnostic chrome and stays off this page only
  // (a class, not `hidden`, so the async freshness loader cannot re-show it).
  const freshnessBar = document.getElementById("data-freshness-bar");
  if (freshnessBar) freshnessBar.classList.toggle("is-dash-hidden", page === "dashboard");

  if (document.body) document.body.classList.toggle("is-revenue-page", revenuePage);

  // PR-ADS-141: Platform Evidence + Lead Intelligence pages use the Evidence
  // window dropdown; the legacy 7d/14d/30d/60d day buttons are hidden there.
  // Every other non-revenue page keeps the day buttons. Evidence pages also
  // collapse the big explainer card into a compact page-truth chip strip.
  applyEvidenceChrome(page);
}

// Toggle the evidence-window dropdown vs the legacy day buttons for a page.
function applyEvidenceChrome(page) {
  const evidencePage = isEvidencePage(page);
  const dayLabel = document.getElementById("time-range-day-label");
  if (dayLabel) dayLabel.hidden = evidencePage;
  document.querySelectorAll(".time-range-btn").forEach((btn) => {
    btn.hidden = evidencePage;
  });
  const evidenceControl = document.getElementById("evidence-window-control");
  if (evidenceControl) evidenceControl.hidden = !evidencePage;
  if (evidencePage) renderEvidenceWindowDropdown();
  if (document.body) document.body.classList.toggle("is-evidence-page", evidencePage);
}

// ── UI threshold state ─────────────────────────────────────────────────────

// Default values matching config/thresholds.yaml doctrine — used as fallback
// when /api/config/ui-thresholds is unavailable.
const DEFAULT_UI_THRESHOLDS = {
  junk_rate: {
    low_pct: 15,   // below this → green
    high_pct: 30,  // above this → red
  },
  spend: {
    high_spend_usd: 100,
  },
  quality_score: {
    strong_min: 8,
    medium_min: 5,
  },
  freshness: {
    stale_after_days: 2,
  },
};

let uiThresholds = DEFAULT_UI_THRESHOLDS;

// Config-driven stale threshold (days). Updated from /api/config/ui-thresholds.
// Default matches config/thresholds.yaml ui.freshness.stale_after_days.
let _staleAfterDays = 2;

// Latest run record fetched by loadDataFreshness() — shared with renderRunMeta().
let _latestRunForMeta = null;

// ── Dataset-to-page mapping (PR-ADS-074) ──────────────────────────────────
//
// Maps each page's sectionKey (used as the run-meta-{sectionKey} element ID)
// to the canonical dataset keys that power it.  Keys are "source/dataset"
// strings matching the sync_state table values returned by /api/datasets/freshness.
//
// Derived pages (ngrams, waste) share their source dataset (google_ads_api/search_terms)
// because no separate freshness record exists for the derived output.
const PAGE_DATASET_MAP = {
  // PR-ADS-143: the Campaign Evidence page depends on BOTH canonical Google Ads
  // daily spend AND HubSpot contacts; the compact freshness reflects the worst of
  // the two, so it never shows fresh when HubSpot contacts are stale/failed.
  campaigns:         ["google_ads_api/canonical_spend", "hubspot/contacts"],
  // PR-ADS-177C: the one Keywords & Search Terms page — the worst of its two
  // sources (the Keywords tab reads durable keyword facts).
  search_terms:      ["google_ads_api/search_terms", "google_ads_api/keyword_facts"],
  ngrams:            ["google_ads_api/search_terms"],   // n-grams derived from search_terms
  // PR-ADS-178B: the Pipeline page reads the CRM funnel spine (leads) and
  // the deal ledger (deals).
  pipeline:          ["hubspot/contact_funnel", "hubspot/lifecycle_events", "hubspot/deals"],
  gclid_attribution: ["gclid/matches"],
};

// Pages whose output is derived/computed from the source datasets above rather
// than being a direct read of the synced table.  Strip copy uses "Derived from"
// wording for these rather than "Dataset freshness:".
const DERIVED_DATASET_PAGES = new Set(["ngrams"]);

// ── Page Explanations (PR-ADS-070) ────────────────────────────────────────
//
// Each page gets a concise explanation block: what it shows, data source,
// dependencies, what empty means, and what action to take next.

const PAGE_EXPLANATIONS = {
  dashboard: {
    title: "Dashboard",
    purpose: "Executive overview of spend, closed-won revenue, pipeline movement, and action signals for one business window.",
    source: "Canonical Revenue Decision Mart — Google Ads spend truth and HubSpot closed-won revenue truth.",
    dependsOn: ["canonical_spend", "leads", "deals", "gclid_attribution"],
    emptyMeans: "No verified spend or closed-won revenue exists in the selected business window.",
    nextAction: "Open Revenue Health to check spend, FX, and revenue truth status."
  },
  campaigns: {
    title: "Campaigns",
    purpose: "Google Ads campaigns over business windows — spend, leads and SQLs (the acquisition cohort), closed-won deals, customers and revenue — plus a separate Meta Ads ad-set table.",
    source: "Canonical Revenue Decision Mart, settled campaign view: Google Ads API campaign spend, HubSpot closed-won revenue, the HubSpot acquisition cohort. Meta ad sets from the Meta ad-set ledger.",
    dependsOn: ["hubspot/deals", "google_ads_api/canonical_spend", "hubspot/contact_funnel"],
    emptyMeans: "No campaign has spend, contacts or closed-won deals in this business window.",
    nextAction: "Check Revenue Health for spend coverage and the deal-ledger sync."
  },
  "search-terms": {
    title: "Keywords & Search Terms",
    purpose: "One page on evidence windows: the selected-window Search Term Universe (queries reported by Google Ads, with factual tri-state review states, waste evidence and pattern analysis) and the Keywords tab (keyword activity, match-type exposure and quality diagnostics).",
    source: "Durable Google Ads API search-term rows and keyword facts, each bounded by source_date; spend is reported search-term / keyword spend, never canonical account spend.",
    dependsOn: ["search_terms", "keyword_facts"],
    emptyMeans: "This does not mean the account is clean. It means no Google Ads API search-term rows are currently available for this window.",
    nextAction: "Check System Status → Search Terms Pipeline."
  },
  ngrams: {
    title: "Patterns",
    purpose: "Recurring 1–3-word patterns derived from the same selected-window Search Term Universe.",
    source: "Derived from Google Ads API search-term evidence — it cannot produce evidence when Search Terms is unavailable.",
    dependsOn: ["search_terms"],
    emptyMeans: "If Search Term Universe is empty or blocked, pattern analysis cannot run.",
    nextAction: "Fix Search Term Universe first."
  },
  // PR-ADS-177C: "keywords" (Keyword Evidence) is the Keywords tab of
  // "search-terms" — its explanation is folded into that entry.
  pipeline: {
    title: "Pipeline",
    purpose: "Leads and closed-won deals together: SQLs (the Google Ads acquisition cohort), the lifecycle funnel (contacts that entered each stage in the window), the closed-won deal list and the customers won.",
    source: "HubSpot lifecycle truth (hubspot_contact_funnel) and the canonical HubSpot closed-won deal ledger.",
    dependsOn: ["contact_funnel", "deals"],
    emptyMeans: "No contact entered a stage and no deal closed-won in the selected window.",
    nextAction: "Check System Status → HubSpot CRM, or run the canonical contact sync."
  },
  "gclid-attribution": {
    title: "GCLID Attribution",
    purpose: "Click-to-lead matching via GCLID — proves which Google Ads click produced which HubSpot contact.",
    source: "GCLID match engine joining Windsor + HubSpot.",
    dependsOn: ["gclid_attribution", "gclid_coverage_snapshots"],
    emptyMeans: "No GCLID attribution rows found. This may mean no matching GCLIDs exist in the selected window.",
    nextAction: "Check System Status → GCLID / Matches."
  },
  scheduler: {
    title: "Data Runs",
    purpose: "Scheduler job status, run history, and next-run schedule.",
    source: "Internal scheduler and run tracking.",
    dependsOn: ["runs"],
    emptyMeans: "No scheduler runs recorded yet. The scheduler may not have executed since run tracking was enabled.",
    nextAction: "Check scheduler configuration and latest job status."
  },
  health: {
    title: "System Status",
    purpose: "War Room — blockers, source health, pipeline status, and data freshness across all datasets.",
    source: "Internal system status and freshness checks.",
    dependsOn: ["system_status"],
    emptyMeans: "System status could not be loaded. Admin access is required.",
    nextAction: "Confirm admin role and API connectivity."
  },
  backfill: {
    title: "Admin Backfill",
    purpose: "Manual historical data ingestion. Dry-run previews the plan without writing to the database.",
    source: "Windsor / HubSpot historical data.",
    dependsOn: ["historical_backfill"],
    emptyMeans: "No backfill run selected. Run a dry-run first to preview the ingestion plan.",
    nextAction: "Select a date range and run dry-run. Dry-run does not write to the database or modify Google Ads/HubSpot. Read-only."
  },
  "roas-countries": {
    title: "Countries",
    purpose: "Revenue attribution by country over business windows — where SQLs, customers, and closed-won revenue come from.",
    source: "Google Ads API = spend/platform evidence. HubSpot = revenue truth (closed-won deals).",
    dependsOn: ["hubspot/deals", "google_ads_api/canonical_geo"],
    emptyMeans: "No attributed revenue found for this business window. Spend may exist without a HubSpot closed-won match, or attribution may be missing/low confidence for these countries.",
    nextAction: "Check that geo spend and HubSpot deal data are synced for the selected window."
  }
};

// ── Page Help Drawer Content (PR-ADS-071) ─────────────────────────────────
//
// Extended help content for each page. Displayed in a slide-out help drawer.
// Each entry: what, source, howToUse, doNotAssume, checkNext.

const PAGE_HELP_CONTENT = {
  dashboard: {
    what: "Executive overview of Google Ads spend, HubSpot closed-won revenue, pipeline conversion, source mix, and computed action signals for one business window.",
    source: "Canonical Revenue Decision Mart (Google Ads spend truth + HubSpot closed-won revenue truth), the source-group breakdown, and the closed-won deal ledger.",
    howToUse: "Pick a business window, scan the hero KPIs and decision cards, then follow the evidence links (Campaigns, Channels) to drill down.",
    doNotAssume: "'Unavailable' means the truth layer withheld a metric (FX, coverage, or lead-date safety) — it is never a $0. Period Revenue / Ad Spend (a non-cohort operational ratio) applies to Google Ads only; other sources are revenue-only.",
    checkNext: "Revenue Health for truth blockers, or Campaigns for the campaigns behind a decision card."
  },
  campaigns: {
    what: "Campaigns — which Google Ads campaigns produce SQLs, closed-won deals, customers and revenue against their spend, over a business window that ends yesterday. Leads and SQLs are the acquisition cohort (Google Ads contacts created in the window); contacts with no campaign label are one residual row. Meta ad sets are a separate table with their own totals.",
    source: "The Revenue Decision Mart's settled campaign view: Google Ads API campaign spend (published once the morning sync has re-read yesterday), HubSpot closed-won deals (campaign-attributable), the HubSpot acquisition cohort. This does NOT use Google Ads conversion value.",
    howToUse: "Pick a business window. The cards and the table are the server's figures; click a campaign for the closed-won deals behind it. Period Revenue / Ad Spend is a non-cohort operational ratio.",
    doNotAssume: "Withheld and Unavailable are never zero. Customers are distinct companies and are never summed across campaigns. Meta ad-set figures are never added to Google Ads; their SQL, deal and revenue cells are not linked yet. Deals closed today are counted on Deals and Overview, not here, until tomorrow.",
    checkNext: "Keywords and Search Terms for query-level evidence, or Revenue Health for blockers."
  },
  // PR-ADS-177C: one Keywords & Search Terms page (owner decision 3,
  // 2026-11-24). The Keyword Evidence help ("keywords") is folded in here.
  "search-terms": {
    what: "Keywords & Search Terms — one page on evidence windows, four tabs. Terms, Flagged and Patterns: Search Terms + Patterns — the selected-window Search Term Universe with factual review states (Flagged waste = durable flag OR safely-confirmed waste evidence · Reviewed clean = every row stored false · Needs review = not yet classified) and waste evidence, plus a Patterns tab of recurring 1–3-word phrases derived from the same terms. Keywords: Keyword Evidence — the selected-window keyword population at the unique Google Ads criterion grain, with match-type exposure, latest-observed quality diagnostics and one factual review signal per keyword.",
    source: "Search terms: Durable Google Ads API search-term rows totalled by their source_date report date for the selected Evidence Window (all_time has no lower bound). Currency is assessed independently per term × campaign: verified Google Ads GBP rows are FX-converted to USD per source-date; unverified legacy rows keep their monetary value withheld (never assumed GBP/USD) and stay visible. Verified Search-Term Spend is therefore a verified subtotal shown as Complete, Partial (excludes N legacy rows) or Unavailable. Reporting Coverage compares FX-safe VERIFIED search-term USD against FX-safe canonical campaign USD; when partial it is verified-row-only. Keywords: Durable Google Ads API keyword facts (keyword_daily_facts) totalled by their source_date report date for the selected Evidence Window (all_time has no lower bound) — never SUM'd across overlapping scheduler snapshots. Verified Keyword Spend is Σ(per-source-date native cost × that date's FX rate); it is a verified subtotal shown Complete, Partial (excludes N unverified/FX-incomplete units) or Unavailable. Quality Score, Expected CTR, Ad Relevance and Landing Page Experience are the LATEST observed Google Ads attributes per criterion — not window averages; a blank score means unavailable, distinct from a genuine 0.",
    howToUse: "Pick an Evidence Window and a tab. Search terms: Pick an Evidence Window, filter by campaign, review state, junk category or minimum spend, and click any row to open its evidence drawer. Switch to Patterns for word-level analysis derived from the same selected-window terms; a term can appear in several patterns, so pattern-row spends overlap and must never be summed. Keywords: Pick an Evidence Window, filter by campaign, match type, criterion status, quality band, review signal or minimum verified spend, and click any row to open its evidence drawer. Export CSV returns the complete server-filtered dataset. Keyword-view reporting coverage compares FX-safe verified keyword-view USD against FX-safe canonical campaign USD — a completeness diagnostic, not a reconciliation failure.",
    howItStaysCurrent: "Changing the Evidence Window is a PostgreSQL query over already-stored facts — it filters instantly and never calls or mutates Google Ads. A scheduled daily sync re-pulls the recent rolling range (so late Google Ads metric and conversion adjustments update the stored facts without duplication), and a one-time historical bootstrap backfills the account's full keyword history so 'All time' can become genuinely complete. 'Partial historical coverage' means the stored durable facts start after the account's history start — earlier dates are not yet backfilled, so 'All time' is a truthful partial, never presented as complete. Admins can trigger a recent-range refresh from System Status → Dataset Freshness; it writes locally only.",
    doNotAssume: "Review states are classification facts, not business outcomes — Reviewed clean does not mean valuable, and a platform conversion is never a confirmed SQL, customer or closed-won deal. Absence of waste evidence is NOT clean — only an explicit stored false is. A flagged term is a human-review candidate, not an approved negative. Attributed SQLs need a directly persisted user query to tie a qualified contact to an exact search term; the CRM stores a HubSpot keyword, not the query, so this normally reads '—' (unavailable) — never a fabricated 0, and a keyword is never treated as a search term. Read-only: no negative keywords are added and nothing is written to Google Ads or HubSpot. The legacy spend_usd column in storage is NOT proven USD — it stores the raw cost_micros/1e6 value in the account's native currency, and the 78 unverified legacy rows are preserved, never relabelled. Review signals are review-prioritisation only — never winner/loser/waste. Zero platform conversions is not confirmed waste, and a platform conversion is never a confirmed SQL, customer or closed-won deal. Broad-match exposure is a factual share, not a recommendation to change match type. Attributed SQLs are HubSpot-confirmed qualified contacts on their created date, uniquely tied to one keyword criterion; a blank ('—') means the SQL could not be uniquely attributed (ambiguous keyword text or unresolved campaign), never zero, and a genuine 0 means uniquely attributable with no matching contact. Read-only: no keywords are added/paused, no bids/match-types changed, no negatives created, nothing written to Google Ads or HubSpot. Before the first successful sync completes, keyword spend reads 'Unavailable' — never a fabricated $0.00. The legacy keywords snapshot table is preserved and never reinterpreted as durable USD.",
    checkNext: "Campaigns for canonical spend, leads, SQLs and closed-won outcomes, or the Flagged tab for the review queue."
  },
  pipeline: {
    what: "Pipeline — leads and closed-won deals for one business window through today. SQLs are the Google Ads acquisition cohort (the same count as Overview and Campaigns). The funnel is the acquisition cohort: contacts created in these days, and how far they have got so far (the same funnel as Overview). The contact list below it is a different count — contacts that entered a stage in the window; pick a stage to list them. The deal list is every closed-won deal; customers are distinct companies.",
    source: "One builder (/api/pipeline): the Revenue Decision Mart's deal view and acquisition cohort, the canonical HubSpot contact funnel, the closed-won deal ledger. This does NOT use Google Ads conversion value.",
    howToUse: "Pick a business window. The Scope and Source selectors filter leads only — the funnel and the leads list; deal figures never change with them. The funnel honours Source and Scope Google Ads-source; under Campaign- or Keyword-attributable it says it cannot be split that way. Filter the deal list by each deal's own source group.",
    doNotAssume: "Withheld and Unavailable are never zero. Customers are never summed and are not the contacts moved to the Customer stage. Open and lost deals are not in the deal ledger.",
    checkNext: "Campaigns for spend against these outcomes, or Revenue Health for blockers."
  },
  "roas-countries": {
    what: "Country-level revenue and spend — estimated until GCLID attribution is fully wired. Shows directional profitability by geography.",
    source: "HubSpot closed-won deal revenue + Google Ads API geo spend. Country Period Revenue / Ad Spend is an estimate because deal-to-country mapping is approximate without full GCLID coverage.",
    howToUse: "Use for directional diagnosis — identify countries that are clearly profitable or clearly losing money. Do not use for precise budget allocation until GCLID attribution is fully wired.",
    doNotAssume: "These are estimates, not exact figures. Country attribution precision will improve as GCLID coverage increases. Do not make final budget decisions based solely on these numbers.",
    checkNext: "GCLID Attribution page to check coverage readiness, or Campaigns for campaign-level precision."
  },
  "gclid-attribution": {
    what: "GCLID click-to-lead matching — a readiness and confidence audit showing how well Google Ads clicks can be traced to HubSpot leads.",
    source: "GCLID match engine joining Windsor click data with HubSpot contact records.",
    howToUse: "Review match rates, coverage percentages, and confidence tiers. This page audits attribution readiness — it does not implement a GCLID-to-revenue bridge.",
    doNotAssume: "This is a readiness/confidence audit only. It does NOT implement actual GCLID bridge attribution, OCT upload, or offline conversion tracking. It measures how ready the data is for those future capabilities.",
    checkNext: "Countries to see how estimates will improve with better coverage, or System Status for GCLID pipeline health."
  },
  scheduler: {
    what: "Scheduler job status — run history, next scheduled runs, and manual trigger buttons for the daily, monthly, incremental-sync and certification jobs.",
    source: "Internal scheduler and run tracking. Ad-platform datasets (campaigns, search terms, keywords, geo) sync from the Google Ads API; lead/deal data syncs from HubSpot. Source labels: Google Ads API, HubSpot, GCLID Attribution, and Windsor legacy.",
    howToUse: "Check that jobs ran recently and completed successfully. Use manual triggers (admin only) to force a run if needed.",
    doNotAssume: "A successful run does not guarantee data quality — it means the job completed without errors. Check specific pages for data validation.",
    checkNext: "System Status for overall pipeline health, or the specific data page to verify output."
  },
  health: {
    what: "War Room — system-wide health dashboard showing blockers, source connectivity, pipeline status, and data freshness across all datasets.",
    source: "Internal system status checks, freshness records, and connectivity probes. The active ad-platform source is the Google Ads API (campaigns, search terms, keywords, geo); Windsor is legacy only.",
    howToUse: "Scan for red/amber indicators. Address blockers first. Freshness shows when each dataset was last successfully synced.",
    doNotAssume: "Green status means the last check passed — it does not guarantee future reliability. Monitor trends, not single readings.",
    checkNext: "Data Runs for detailed run logs, or the specific blocked page for investigation."
  },
  backfill: {
    what: "Manual historical data ingestion — run backfills to pull historical data from Windsor/HubSpot for past time periods.",
    source: "Windsor / HubSpot historical APIs, writing to the local database.",
    howToUse: "Select a date range. Run dry-run first to preview what will be ingested. Then execute the actual backfill if the plan looks correct.",
    doNotAssume: "Backfill writes to the local database only. It does NOT modify Google Ads, HubSpot, or any external system. Dry-run is read-only.",
    checkNext: "System Status to verify post-backfill freshness, or the relevant data page to confirm new rows."
  }
};

// ── Page Dependencies (PR-ADS-070) ────────────────────────────────────────
//
// Maps each page to the source-qualified dataset keys it depends on for
// canonical freshness checks.  Keys match the "source/dataset" format used
// by PAGE_DATASET_MAP and _datasetFreshnessByKey so that
// getPageCanonicalStatus() can look them up directly.
// Pages with no canonical freshness record (scheduler, health, backfill)
// use empty arrays — they have their own status mechanisms.

const PAGE_DEPENDENCIES = {
  dashboard:               ["google_ads_api/canonical_spend", "hubspot/contacts", "hubspot/deals", "google_ads_api/search_terms"],
  // PR-ADS-177B: the one Campaigns page — deals, campaign spend and the
  // acquisition cohort (the contact funnel).
  campaigns:               ["hubspot/deals", "google_ads_api/canonical_spend", "hubspot/contact_funnel"],
  // PR-ADS-177C: the Keywords tab reads durable keyword facts.
  "search-terms":          ["google_ads_api/search_terms", "google_ads_api/keyword_facts"],
  ngrams:                  ["google_ads_api/search_terms"],
  // PR-ADS-178B: the one Pipeline page — the contact funnel and the deal ledger.
  pipeline:                ["hubspot/contact_funnel", "hubspot/deals"],
  "gclid-attribution":     ["gclid/matches"],
  scheduler:               [],
  health:                  [],
  backfill:                [],
  "roas-countries":          ["hubspot/deals", "google_ads_api/canonical_geo"],
};

// ── Session state ──────────────────────────────────────────────────────────

let _currentUser   = null;  // { username, role } or null
let _currentPage   = null;  // active page id string
const VALID_DAY_WINDOWS = [7, 14, 30, 60, 90, 365];

// ── Evidence windows (PR-ADS-141) ──────────────────────────────────────────
// Platform Evidence (Campaigns, Search Terms, Keywords, Countries) and Lead
// Intelligence (Lead Quality, In Progress Leads, Flagged Waste Terms) pages use
// an *evidence window* dropdown — distinct from the *business windows* used by
// the Dashboard and Revenue & Attribution pages. The two vocabularies never mix.
const EVIDENCE_WINDOWS = [
  { value: "7d",       label: "7 days",   days: 7 },
  { value: "14d",      label: "14 days",  days: 14 },
  { value: "30d",      label: "30 days",  days: 30 },
  { value: "60d",      label: "60 days",  days: 60 },
  { value: "180d",     label: "180 days", days: 180 },
  { value: "all_time", label: "All time", days: null },
];
const DEFAULT_EVIDENCE_WINDOW = "30d";

// Routes that use the evidence window dropdown. (UI names differ from routes:
// countries=geo, lead-quality=leads, in-progress-leads=opportunities.)
// N-Grams is derived Search Term evidence, so it uses the same Evidence window
// dropdown (its request is window-driven).
// PR-ADS-153C: Leads moved to business windows; In Progress Leads retired.
// PR-ADS-153D: Flagged Waste Terms retired into Search Terms → Flagged, which
// is served by the Search Terms page's own evidence window — the old page must
// not leave a second window implementation behind.
// PR-ADS-176B: Countries left this list with the #/geo page — it is on
// business windows now; the 7/14/30/60/180-day evidence windows are gone from it.
// PR-ADS-177B: Campaigns left this list — it is on business windows now.
// PR-ADS-177C: "keywords" is the Keywords tab of "search-terms" now.
const EVIDENCE_PAGES = [
  "search-terms", "ngrams",
];

function isEvidencePage(page) {
  return EVIDENCE_PAGES.includes(page);
}

let _selectedDays  = (() => {
  try {
    const stored = sessionStorage.getItem("ads_days");
    const n = stored ? parseInt(stored, 10) : 30;
    return VALID_DAY_WINDOWS.includes(n) ? n : 30;
  } catch (_) {
    return 30;
  }
})();  // time range selector — tab-scoped via sessionStorage

// Keywords page state
let keywordRows = [];
let keywordLoadStatus = "idle"; // idle | loading | ok | empty | db_unavailable | error

// Search Terms page state lives with the PR-ADS-144 module (see
// "Search Terms + Patterns evidence page" section).

// GCLID Attribution page state
let gclidRows = [];
let gclidNextCursor = null;
let gclidHasMore = false;
let gclidStatus = "idle"; // idle | loading | ok | empty | db_unavailable | cursor_error | error
let gclidCoverageRows = [];
let gclidCoverageStatus = "idle"; // idle | loading | ok | empty | unavailable

// GCLID Attribution quality state (PR-ADS-048)
let gclidQualityStatus = "idle"; // idle | loading | ok | empty | db_unavailable | error
let gclidQualityData = null;

// GCLID Attribution page prefill (set when another page navigates here with a campaign)
let gclidAttributionPrefill = null;

// N-Gram page prefill (set when another page navigates here with a campaign)
let ngramsPrefill = null;

// Dataset Freshness state
let datasetFreshnessRows = [];
let datasetFreshnessStatus = "idle"; // idle | loading | ok | empty | db_unavailable | error

// Per-dataset freshness cache — populated by loadDatasetFreshness() and used by
// renderPageDatasetFreshness().  Keyed by "source/dataset" (e.g. "google_ads_api/canonical_spend").
let _datasetFreshnessByKey = {};

// ── Utility helpers ────────────────────────────────────────────────────────

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fmt(value) {
  if (value === null || value === undefined || value === "") return "—";
  return escapeHtml(String(value));
}

function fmtDate(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("en-GB", {
      day: "2-digit", month: "short", year: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  } catch (_) {
    return escapeHtml(iso);
  }
}

function formatRelativeAge(isoStr) {
  if (!isoStr) return "";
  try {
    const ageDays = Math.floor((Date.now() - new Date(isoStr).getTime()) / 86400000);
    if (ageDays === 0) return "Updated today";
    if (ageDays === 1) return "Updated 1 day ago";
    return `Updated ${ageDays} days ago`;
  } catch (_) {
    return "";
  }
}

function fmtDollar(n) {
  if (n === null || n === undefined) return "—";
  if (n >= 1000) return "$" + (n / 1000).toFixed(1) + "k";
  return "$" + n.toFixed(0);
}

/**
 * Neutralize formula-injection: prefix cells that start with =, +, -, or @ with a
 * single quote so spreadsheet apps don't interpret them as formulas.
 * Does not mutate UI data — called only during serialisation.
 */
function sanitizeCsvCell(value) {
  const s = value === null || value === undefined ? "" : String(value);
  if (/^\s*[=+\-@]/.test(s)) {
    return "'" + s;
  }
  return s;
}

/**
 * Trigger a CSV download in the browser.
 * @param {string} filename - The suggested download filename (e.g. "ngrams.csv").
 * @param {string[]} headers - Column header labels.
 * @param {(string|number|null|undefined)[][]} rowData - 2-D array of values, one sub-array per row.
 */
function downloadCSV(filename, headers, rowData) {
  const escape = (v) => {
    const s = sanitizeCsvCell(v);
    // Quote fields that contain commas, quotes, newlines, or carriage returns (RFC 4180)
    if (s.includes(",") || s.includes('"') || s.includes("\n") || s.includes("\r")) {
      return '"' + s.replace(/"/g, '""') + '"';
    }
    return s;
  };
  const lines = [headers.map(escape).join(",")];
  rowData.forEach((row) => lines.push(row.map(escape).join(",")));
  // Prepend UTF-8 BOM so Excel on Windows decodes non-Latin text (e.g. Arabic) correctly
  const csv  = "\ufeff" + lines.join("\r\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement("a");
  a.href     = url;
  a.download = filename;
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { document.body.removeChild(a); URL.revokeObjectURL(url); }, 1000);
}

// ── Page Explanation & Empty State helpers (PR-ADS-070) ────────────────────

/**
 * Render a compact page explanation panel for the given page key.
 * Inserts into an element with id="page-explanation-{pageKey}" if it exists,
 * otherwise returns the HTML string.
 */
function renderPageExplanation(pageKey, options = {}) {
  // PR-ADS-143: the Campaign Evidence page keeps its source/methodology in the Help
  // drawer, not a permanent strip above the table — so no inline explanation chips.
  if (pageKey === "campaigns" && !options.forceVisible) {
    const el = document.getElementById("page-explanation-campaigns");
    if (el) { el.hidden = true; el.innerHTML = ""; }
    return "";
  }
  const info = PAGE_EXPLANATIONS[pageKey];
  if (!info) return "";

  const deps = (info.dependsOn || []).join(", ") || "None";
  const showChips = options.showChips !== false;

  let chipsHtml = "";
  if (showChips) {
    const chipItems = [];
    if (info.source) chipItems.push(`<span class="context-chip context-chip--source">${escapeHtml(info.source)}</span>`);
    if (info.dependsOn && info.dependsOn.length > 0) {
      chipItems.push(`<span class="context-chip context-chip--depends">Depends on: ${escapeHtml(deps)}</span>`);
    }
    // Pages that can trigger actions (scheduler runs, backfill) don't get the read-only chip
    const NON_READONLY_PAGES = new Set(["scheduler", "backfill"]);
    if (!NON_READONLY_PAGES.has(pageKey)) {
      chipItems.push(`<span class="context-chip context-chip--readonly">Read-only</span>`);
    }
    chipsHtml = `<div class="page-context-chips">${chipItems.join("")}</div>`;
  }

  const html = `
    <div class="page-explanation">
      <div class="page-explanation__grid">
        <div class="page-explanation__item">
          <strong>What this page shows</strong>
          <span>${escapeHtml(info.purpose)}</span>
        </div>
        <div class="page-explanation__item">
          <strong>What empty means</strong>
          <span>${escapeHtml(info.emptyMeans)}</span>
        </div>
        <div class="page-explanation__item">
          <strong>Next action</strong>
          <span>${escapeHtml(info.nextAction)}</span>
        </div>
      </div>
      ${chipsHtml}
    </div>`;

  // Try to inject into a dedicated container if present
  const container = document.getElementById(`page-explanation-${pageKey}`);
  if (container) {
    container.innerHTML = html;
    container.hidden = false;
  }
  return html;
}

/**
 * Build a contextual empty-state HTML block based on canonical status and page context.
 *
 * @param {object} params
 * @param {string} params.pageKey - Page route key
 * @param {string} [params.canonicalStatus] - Canonical freshness status
 * @param {number} [params.rowsInWindow] - Row count in current window
 * @param {boolean} [params.filtersActive] - Whether user filters narrowed results to zero
 * @param {string} [params.error] - Error message if applicable
 * @returns {string} HTML string
 */
function buildEmptyState({ pageKey, canonicalStatus, rowsInWindow, filtersActive, error }) {
  const info = PAGE_EXPLANATIONS[pageKey] || {};
  const title = info.title || pageKey;

  // Determine empty state type — PR-ADS-095 wires in refined states so we
  // avoid falling back to "unknown" for known canonical states.
  let stateType = "unknown";
  if (error) {
    stateType = "error";
  } else if (filtersActive) {
    stateType = "filtered_out";
  } else if (canonicalStatus === "dependency_blocked" || canonicalStatus === "blocked_by_dependency") {
    stateType = "dependency_blocked";
  } else if (canonicalStatus === "fresh_but_empty" || canonicalStatus === "empty_success") {
    stateType = "fresh_but_empty";
  } else if (canonicalStatus === "failed" || canonicalStatus === "failed_no_data" || canonicalStatus === "stale_and_empty") {
    stateType = "stale_or_failed";
  } else if (canonicalStatus === "partial_no_data") {
    stateType = "partial_no_data";
  } else if (canonicalStatus === "data_available_latest_sync_failed") {
    stateType = "degraded_sync_failed";
  } else if (canonicalStatus === "data_available_latest_sync_partial") {
    stateType = "degraded_sync_partial";
  } else if (canonicalStatus === "stale_with_data") {
    stateType = "stale_with_data";
  } else if (canonicalStatus === "db_unavailable") {
    stateType = "db_unavailable";
  } else if (canonicalStatus === "not_run_but_derivable") {
    stateType = "not_run_but_derivable";
  } else if (canonicalStatus === "not_run_no_upstream_data") {
    stateType = "not_run_no_upstream_data";
  } else if (canonicalStatus === "not_run") {
    stateType = "not_run";
  } else if (canonicalStatus === "row_count_not_enabled") {
    stateType = "row_count_not_enabled";
  } else if (canonicalStatus === "unknown_row_count") {
    stateType = "unknown_row_count";
  } else if (rowsInWindow === 0) {
    stateType = "no_rows";
  }

  const severityClass = {
    error: "empty-state--error",
    dependency_blocked: "empty-state--blocked",
    stale_or_failed: "empty-state--error",
    db_unavailable: "empty-state--error",
    fresh_but_empty: "empty-state--warning",
    degraded_sync_failed: "empty-state--warning",
    degraded_sync_partial: "empty-state--warning",
    partial_no_data: "empty-state--error",
    stale_with_data: "empty-state--warning",
    filtered_out: "empty-state--info",
    not_run: "empty-state--info",
    not_run_but_derivable: "empty-state--warning",
    not_run_no_upstream_data: "empty-state--blocked",
    row_count_not_enabled: "empty-state--info",
    unknown_row_count: "empty-state--info",
    no_rows: "empty-state--info",
    unknown: "empty-state--info",
  }[stateType] || "empty-state--info";

  let body = "";
  let actionText = info.nextAction || "";

  switch (stateType) {
    case "error":
      body = error || `Could not load ${title} data.`;
      break;
    case "filtered_out":
      body = "No results match the current filter.";
      actionText = "Try adjusting or clearing filters.";
      break;
    case "dependency_blocked":
      body = `${title} is blocked by a dependency.`;
      if (info.emptyMeans) body += ` ${info.emptyMeans}`;
      break;
    case "fresh_but_empty":
      body = info.emptyMeans || `No ${title} rows found for this window.`;
      break;
    case "stale_or_failed":
      body = `${title} data may be stale or the pipeline has failed.`;
      actionText = info.nextAction || "Check System Status for pipeline health.";
      break;
    case "degraded_sync_failed":
      body = `${title} latest sync failed, but usable rows still exist. The page is degraded, not blocked.`;
      actionText = "Review latest sync error in System Status.";
      break;
    case "degraded_sync_partial":
      body = `${title} latest sync completed partially — it did not reach the end of its result set — so these rows may be an incomplete population.`;
      actionText = "Re-run the sync to completion before relying on counts here.";
      break;
    case "partial_no_data":
      body = `${title} latest sync completed partially and returned nothing for this window. This window is not proven empty — it was never fully read.`;
      actionText = "Re-run the sync to completion, then re-check.";
      break;
    case "stale_with_data":
      body = `${title} has data, but the latest sync is older than the freshness threshold.`;
      actionText = "Trigger a fresh sync or check the scheduler schedule.";
      break;
    case "db_unavailable":
      body = `${title} temporarily unavailable — database offline.`;
      actionText = "Check System Status for database connectivity.";
      break;
    case "not_run":
      body = info.emptyMeans || `${title} has not run yet.`;
      break;
    case "not_run_but_derivable":
      body = `${title} has not run yet, but upstream data is available — analysis can be generated.`;
      actionText = "Run the derived analysis from existing upstream data.";
      break;
    case "not_run_no_upstream_data":
      body = `${title} has not run, and upstream data is also unavailable.`;
      actionText = "Run the upstream source sync first; this analysis becomes available after.";
      break;
    case "row_count_not_enabled":
      body = `${title} does not yet expose a row-count diagnostic for this window.`;
      actionText = "Add a row-count query if this page depends on freshness.";
      break;
    case "unknown_row_count":
      body = `${title} row-count query is unavailable.`;
      actionText = "Check row-count support for this dataset.";
      break;
    default:
      body = info.emptyMeans || `No ${title} data found for this window.`;
  }

  return `
    <div class="empty-state-block ${severityClass}">
      <p class="empty-state__title">${escapeHtml(body)}</p>
      ${actionText ? `<p class="empty-state__body"><strong>Next:</strong> ${escapeHtml(actionText)}</p>` : ""}
    </div>`;
}

/**
 * Determine the most severe canonical freshness status for a page, based on
 * its PAGE_DEPENDENCIES entries looked up in the _datasetFreshnessByKey cache.
 *
 * @param {string} pageKey - Page route key (e.g. "search-terms", "waste")
 * @returns {string|null} Canonical status string or null when undeterminable
 */
function getPageCanonicalStatus(pageKey) {
  const deps = PAGE_DEPENDENCIES[pageKey];
  if (!deps || deps.length === 0) return null;
  if (Object.keys(_datasetFreshnessByKey).length === 0) return null;

  // Severity order, most severe first — first match wins.
  // PR-ADS-095 refined states are interleaved so the page roll-up picks the
  // refined name (e.g. "data_available_latest_sync_failed") not "unknown".
  const SEVERITY_ORDER = [
    "db_unavailable",
    "failed_no_data",
    "failed",
    // A truncated sync that left nothing is as blocking as a failed one: the
    // window is not proven empty, it was never fully read.
    "partial_no_data",
    "stale_and_empty",
    "not_run_no_upstream_data",
    "blocked_by_dependency",
    "dependency_blocked",
    "fresh_but_empty",
    "empty_success",
    "data_available_latest_sync_failed",
    "data_available_latest_sync_partial",
    "stale_with_data",
    "not_run_but_derivable",
    "running",
    "not_run",
    "row_count_not_enabled",
    "unknown_row_count",
    "fresh_with_data",
  ];

  let mostSevereIdx = SEVERITY_ORDER.length; // beyond list = no data / unknown
  let worstStatus = null;

  for (const key of deps) {
    const row = _datasetFreshnessByKey[key];
    if (!row || !row.canonical_status) continue;
    const idx = SEVERITY_ORDER.indexOf(row.canonical_status);
    if (idx !== -1 && idx < mostSevereIdx) {
      mostSevereIdx = idx;
      worstStatus = row.canonical_status;
    }
  }

  return worstStatus;
}

function verdictBadge(verdict) {
  const v = verdict ? verdict.toUpperCase() : "";
  const cls = ["SCALE", "FIX", "HOLD", "CUT"].includes(v)
    ? `verdict-badge--${v}`
    : "verdict-badge--HOLD";
  return `<span class="verdict-badge ${cls}">${escapeHtml(v || "—")}</span>`;
}

function statusBadge(status) {
  const map = {
    ok: "badge--ok", pass: "badge--ok", success: "badge--ok",
    fail: "badge--error", failed: "badge--error", error: "badge--error",
    running: "badge--running",
    empty: "badge--warning", warning: "badge--warning", pending: "badge--warning",
    loading: "badge--loading",
  };
  const lower = (status || "").toLowerCase();
  const cls = map[lower] || "badge--neutral";
  return `<span class="badge ${cls}"><span class="dot"></span>${escapeHtml(status || "unknown")}</span>`;
}

// Returns true when a lead's contact_id is a usable dedup key (non-null, non-empty).
function hasValidContactId(lead) {
  return lead.contact_id !== null &&
         lead.contact_id !== undefined &&
         lead.contact_id !== "";
}

// Normalise a run record's status, accounting for in-progress rows.
// The DB inserts runs with status='failed' at start and updates on completion;
// a row with no finished_at and a non-success status is therefore still running.
function normalizeRunStatus(run) {
  const raw = (run.status || "unknown").toLowerCase();
  if (!run.finished_at && raw !== "success") return "running";
  if (["failed", "error", "fail"].includes(raw)) return "failed";
  if (raw === "success") return "success";
  return raw || "unknown";
}

// ── Time range selector ────────────────────────────────────────────────────

function getSelectedDays() {
  return _selectedDays;
}

function getCurrentDays() {
  const d = _selectedDays || 60;
  return VALID_DAY_WINDOWS.includes(d) ? d : 60;
}

function getCurrentWindowParam() {
  return `${getCurrentDays()}d`;
}

function setSelectedDays(days) {
  const normalized = VALID_DAY_WINDOWS.includes(Number(days)) ? Number(days) : 60;
  _selectedDays = normalized;
  try { sessionStorage.setItem("ads_days", String(normalized)); } catch (_) { /* ignore */ }
  // Update active button state
  updateWindowControls(normalized);
  // Reload current page with new window
  if (_currentPage) loadPage(_currentPage);
  loadDataFreshness();
}

function updateWindowControls(days) {
  document.querySelectorAll(".time-range-btn").forEach((btn) => {
    btn.classList.toggle("active", parseInt(btn.dataset.days, 10) === days);
  });
  // Sync any local ROAS window dropdowns to match global
  document.querySelectorAll("[data-sync-window-select]").forEach((select) => {
    select.value = `${days}d`;
  });
}

function windowParamToDays(value) {
  const n = parseInt(String(value || "").replace("d", ""), 10);
  return VALID_DAY_WINDOWS.includes(n) ? n : getCurrentDays();
}

function handleSyncedWindowSelectChange(e) {
  const days = windowParamToDays(e.target.value);
  setSelectedDays(days);
}

// ── Evidence window selector (PR-ADS-141) ───────────────────────────────────
// The evidence window is a string key (7d/14d/30d/60d/180d/all_time) used by the
// Platform Evidence + Lead Intelligence page loaders. It is sent to the backend
// as `window=<key>` — the endpoint resolves it (all_time = no lower date bound)
// and never silently coerces an unknown value.

let _evidenceWindow = (() => {
  try {
    const stored = sessionStorage.getItem("evidence_window");
    return EVIDENCE_WINDOWS.some((w) => w.value === stored) ? stored : DEFAULT_EVIDENCE_WINDOW;
  } catch (_) {
    return DEFAULT_EVIDENCE_WINDOW;
  }
})();

function getEvidenceWindow() {
  return _evidenceWindow;
}

// The definition ({value,label,days}) for a window key (defaults to current).
function evidenceWindowDef(value) {
  const key = value || _evidenceWindow;
  return EVIDENCE_WINDOWS.find((w) => w.value === key)
    || EVIDENCE_WINDOWS.find((w) => w.value === DEFAULT_EVIDENCE_WINDOW);
}

// Query fragment for evidence endpoints: `window=<key>` (never a fake days value).
function evidenceWindowQuery() {
  return `window=${encodeURIComponent(getEvidenceWindow())}`;
}

function setEvidenceWindow(value) {
  const def = EVIDENCE_WINDOWS.find((w) => w.value === value);
  const normalized = def ? def.value : DEFAULT_EVIDENCE_WINDOW;
  _evidenceWindow = normalized;
  try { sessionStorage.setItem("evidence_window", normalized); } catch (_) { /* ignore */ }
  syncEvidenceWindowControls(normalized);
  // Close open drawers so stale detail from the previous window is dropped
  // (PR-ADS-146A §4). The window change is a DB query — it re-invokes the active
  // page loader (/api/keyword-evidence?window=…) and never calls Google Ads.
  if (typeof closeKwDrawer === "function") closeKwDrawer();
  if (_currentPage) loadPage(_currentPage);
  loadDataFreshness();
}

// Populate the evidence dropdown options (idempotent) and select the current one.
function renderEvidenceWindowDropdown() {
  const select = document.getElementById("evidence-window-select");
  if (!select) return;
  if (select.options.length !== EVIDENCE_WINDOWS.length) {
    select.innerHTML = EVIDENCE_WINDOWS
      .map((w) => `<option value="${w.value}">${w.label}</option>`)
      .join("");
  }
  select.value = getEvidenceWindow();
}

function syncEvidenceWindowControls(value) {
  const select = document.getElementById("evidence-window-select");
  if (select && select.value !== value) select.value = value;
}

function handleEvidenceWindowSelectChange(e) {
  setEvidenceWindow(e.target.value);
}

// ── Modern evidence page shell (PR-ADS-141 foundation) ──────────────────────
// Reusable, truth-safe chrome for the Platform Evidence + Lead Intelligence
// pages. This PR lays down the shared component + CSS; the page-by-page revamps
// (PR-ADS-142+) render their content into it. It never fabricates values — any
// missing field renders "Unavailable", never null/undefined/NaN/$0.

// One compact status/context chip. `tone` ∈ readonly|source|ok|warning|info.
function evidenceChip(label, tone = "info") {
  return `<span class="evidence-status-chip evidence-status-chip--${tone}">${escapeHtml(label)}</span>`;
}

// Compact "Page truth" strip: what the page is + its source + read-only, as
// chips (never a big diagnostic wall). Derived from PAGE_EXPLANATIONS so it stays
// consistent with the Help drawer.
function renderEvidenceTruthStrip(pageKey) {
  const info = (typeof PAGE_EXPLANATIONS === "object" && PAGE_EXPLANATIONS[pageKey]) || {};
  const chips = [];
  if (info.title) chips.push(evidenceChip(info.title, "info"));
  if (info.source) chips.push(evidenceChip(info.source, "source"));
  chips.push(evidenceChip("Read-only", "readonly"));
  return `<div class="evidence-truth-strip">${chips.join("")}</div>`;
}

// Modern page shell wrapper. `truthChips` (array of {label,tone}) and a
// `dataStatus` chip render in the compact command header; `children` is the
// page body HTML.
function renderEvidencePageShell({ title, subtitle, truthChips = [], dataStatus = null, children = "" } = {}) {
  const chips = truthChips.map((c) => evidenceChip(c.label, c.tone || "info")).join("");
  const statusHtml = dataStatus ? evidenceChip(dataStatus.label, dataStatus.tone || "info") : "";
  return `
    <section class="evidence-shell">
      <header class="evidence-command-bar">
        <div class="evidence-command-bar__head">
          <h2 class="evidence-command-bar__title">${escapeHtml(title || "")}</h2>
          ${subtitle ? `<p class="evidence-command-bar__sub">${escapeHtml(subtitle)}</p>` : ""}
        </div>
        <div class="evidence-command-bar__chips">${chips}${statusHtml}</div>
      </header>
      <div class="evidence-shell__body">${children}</div>
    </section>`;
}

// ── Fetch helpers ──────────────────────────────────────────────────────────

async function fetchJSON(url, options = {}) {
  const res = await fetch(url, { credentials: "same-origin", ...options });
  if (res.status === 401) {
    showLogin();
    throw new Error("HTTP 401");
  }
  if (res.status === 403) throw new Error("HTTP 403");
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

async function loadUiThresholds() {
  try {
    const data = await fetchJSON("/api/config/ui-thresholds");
    uiThresholds = {
      ...DEFAULT_UI_THRESHOLDS,
      ...data,
      junk_rate: {
        ...DEFAULT_UI_THRESHOLDS.junk_rate,
        ...(data.junk_rate || {}),
      },
      spend: {
        ...DEFAULT_UI_THRESHOLDS.spend,
        ...(data.spend || {}),
      },
      quality_score: {
        ...DEFAULT_UI_THRESHOLDS.quality_score,
        ...(data.quality_score || {}),
      },
      freshness: {
        ...DEFAULT_UI_THRESHOLDS.freshness,
        ...(data.freshness || {}),
      },
    };
    // Update the config-driven stale threshold used by the freshness bar and per-page strips.
    const sad = uiThresholds.freshness && uiThresholds.freshness.stale_after_days;
    if (typeof sad === "number" && sad >= 1) _staleAfterDays = sad;
  } catch (err) {
    console.warn("[loadUiThresholds] failed, using defaults:", err);
    uiThresholds = DEFAULT_UI_THRESHOLDS;
  }
}

// ── Auth flow ──────────────────────────────────────────────────────────────

function showLogin() {
  document.getElementById("login-screen").style.display = "flex";
  document.getElementById("app").style.display = "none";
  _currentUser = null;
  // Ensure the drawers/overlays are hidden so they don't block the login screen.
  if (typeof closeKwDrawer === "function") closeKwDrawer();
}

function showApp(user) {
  document.getElementById("login-screen").style.display = "none";
  document.getElementById("app").style.display = "flex";
  _currentUser = user;
  applySidebarUser(user);
  // Show/hide System Health nav item
  const healthNav = document.getElementById("nav-health-item");
  if (healthNav) healthNav.hidden = user.role !== "admin";
  // Show/hide System nav item (admin-only — PR-ADS-162C)
  const systemNav = document.getElementById("nav-system-item");
  if (systemNav) systemNav.hidden = user.role !== "admin";
  // Show/hide Historical Backfill nav item
  const backfillNav = document.getElementById("nav-backfill-item");
  if (backfillNav) backfillNav.hidden = user.role !== "admin";
  // Show/hide Revenue Health nav item (admin-only — PR-ADS-113)
  const revenueHealthNav = document.getElementById("nav-revenue-health-item");
  if (revenueHealthNav) revenueHealthNav.hidden = user.role !== "admin";
  // PR-ADS-174A: each menu item opens the first page this viewer may see, and
  // an item with no such page is not shown.
  applyNavGating();
  // PR-ADS-124: apply chrome immediately from the URL so revenue pages never
  // flash the ad-window bar / monitoring banner before the first navigate().
  applyPageChrome(hashToPage(window.location.hash));
  // Start with sidebar health check and data freshness
  loadSidebarHealth();
  loadDataFreshness();
  loadMonitoringStatus();
  // Pre-fetch dataset freshness cache so per-page strips are ready before the
  // user navigates to any data page.  loadDatasetFreshness() guards against
  // missing DOM elements so it is safe to call outside the Health page.
  loadDatasetFreshness();
  // Re-assert chrome after the async loaders are kicked off (belt-and-suspenders
  // for the monitoring race): the current URL decides, not the fetch timing.
  applyPageChrome(hashToPage(window.location.hash));
}

function applySidebarUser(user) {
  const nameEl = document.getElementById("sidebar-user-name");
  const roleEl = document.getElementById("sidebar-user-role");
  if (nameEl) nameEl.textContent = user.username;
  if (roleEl) {
    roleEl.textContent = user.role;
    roleEl.className = `sidebar__role sidebar__role--${user.role}`;
  }
}

async function checkAuth() {
  try {
    const res = await fetch("/auth/me", { credentials: "same-origin" });
    if (!res.ok) { showLogin(); return false; }
    const user = await res.json();
    showApp(user);
    return true;
  } catch (_) {
    showLogin();
    return false;
  }
}

async function handleLogin(e) {
  e.preventDefault();
  const usernameEl = document.getElementById("login-username");
  const passwordEl = document.getElementById("login-password");
  const errorEl    = document.getElementById("login-error");
  const submitBtn  = document.getElementById("login-submit-btn");

  const username = usernameEl ? usernameEl.value.trim() : "";
  const password = passwordEl ? passwordEl.value : "";

  if (!username || !password) {
    showLoginError(errorEl, "Username and password are required.");
    return;
  }

  if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = "Signing in…"; }
  if (errorEl) errorEl.hidden = true;

  try {
    const res = await fetch("/auth/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });

    if (res.ok) {
      const user = await res.json();
      if (passwordEl) passwordEl.value = "";
      showApp(user);
      navigate("dashboard", { updateHash: true, replace: true });
      // Load thresholds in background — do not block first page render.
      loadUiThresholds().then(() => {
        if (_currentPage) loadPage(_currentPage);
      }).catch((err) => {
        console.warn("[loadUiThresholds] background load failed; continuing with defaults.", err);
      });
    } else {
      const body = await res.json().catch(() => ({}));
      showLoginError(errorEl, body.detail || "Invalid username or password.");
    }
  } catch (_) {
    showLoginError(errorEl, "Login failed — please try again.");
  } finally {
    if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = "Sign in"; }
  }
}

function showLoginError(el, msg) {
  if (!el) return;
  el.textContent = msg;
  el.hidden = false;
}

async function handleLogout() {
  try {
    await fetch("/auth/logout", { method: "POST", credentials: "same-origin" });
  } catch (_) { /* ignore */ }
  _currentUser = null;
  showLogin();
}

// ── Hash Routing (PR-ADS-094) ──────────────────────────────────────────────

const HASH_ROUTE_ALIASES = {
  "ngrams": "search-terms",
  "countries": "roas-countries",
  "data-runs": "scheduler",
  "system-status": "health",
};

function pageToHash(page) {
  return `#/${page}`;
}

function hashToPage(hash) {
  // Strip any query suffix (e.g. #/dashboard?tab=revenue) before resolving the
  // page key — dashboard tab state (PR-ADS-135) travels as a ?tab= param.
  const raw = (hash || "").replace(/^#\/?/, "").split("?")[0].trim();
  if (!raw) return "dashboard";
  // Resolve aliases
  if (HASH_ROUTE_ALIASES[raw]) return HASH_ROUTE_ALIASES[raw];
  return raw;
}

// Dashboard inner-tab state (PR-ADS-135). Overview is the default; Revenue is
// linkable/bookmarkable via #/dashboard?tab=revenue.
// PR-ADS-181A: "channels" is retired (the Channels page is the one channel
// surface); hashToDashTab sends a bookmarked ?tab=channels there.
const DASHBOARD_TABS = ["overview", "revenue", "campaigns", "countries", "deals"];

function hashToDashTab(hash) {
  const q = (hash || "").split("?")[1] || "";
  const m = q.split("&").map((p) => p.split("=")).find((kv) => kv[0] === "tab");
  const tab = m ? decodeURIComponent(m[1] || "") : "";
  return DASHBOARD_TABS.includes(tab) ? tab : "overview";
}

function navigateToHash(page, options) {
  options = options || {};
  let hash = pageToHash(page);
  // Preserve a bookmarked dashboard tab (#/dashboard?tab=revenue) so navigating
  // to the dashboard does not strip the tab before the loader reads it.
  if (page === "dashboard") {
    const tab = hashToDashTab(window.location.hash);
    if (tab !== "overview") hash = `${hash}?tab=${tab}`;
  }
  // Same for the Search Terms tab deep links (#/search-terms?tab=flagged and
  // ?tab=patterns). The legacy #/ngrams route lands on Patterns (PR-ADS-144)
  // and the retired #/waste route lands on Flagged (PR-ADS-153D), so both keep
  // their destination through the hash rewrite.
  if (page === "search-terms") {
    const raw = window.location.hash || "";
    const q = new URLSearchParams(raw.split("?")[1] || "");
    const wanted = raw.startsWith("#/ngrams") ? "patterns"
      : (_stPendingTab || (ST_TABS.includes(q.get("tab")) ? q.get("tab") : null));
    if (wanted && wanted !== "terms") {
      // q.get() returns a DECODED value, so it must be re-encoded on the way
      // back into the hash — a term containing a space, "&" or "%" would
      // otherwise produce a malformed link or inject a second query param.
      const term = q.get("term");
      hash = `${hash}?tab=${wanted}` +
        (term ? `&term=${encodeURIComponent(term)}` : "");
    }
  }
  // PR-ADS-181A: the Channels period travels as ?window= so a refresh (or a
  // shared link) holds it; the default period needs none.
  if (page === "channels") {
    hash = channelsHashFor(channelsWindowFromHash(window.location.hash) || _channelsWindow);
  }
  // PR-ADS-161E: "View blockers" deep links carry the CRM Truth Exceptions
  // filters (#/health?cte_window=business/ytd&cte_family=customer_identity).
  if (page === "health") {
    const q = new URLSearchParams((window.location.hash || "").split("?")[1] || "");
    const keep = new URLSearchParams();
    ["cte_window", "cte_family", "cte_reason"].forEach((k) => {
      if (q.get(k)) keep.set(k, q.get(k));
    });
    if (keep.toString()) hash = `${hash}?${keep.toString()}`;
  }
  if (window.location.hash !== hash) {
    if (options.replace) {
      history.replaceState(null, "", hash);
    } else {
      history.pushState(null, "", hash);
    }
  }
}

// ── Router ─────────────────────────────────────────────────────────────────

function navigate(page, options) {
  options = options || {};
  const updateHash = options.updateHash !== false;

  // PR-ADS-153C: retired pages redirect to their canonical destination and
  // carry the old page's filter intent, so no old link becomes a dead end.
  // PR-ADS-181A: so does the retired Dashboard → Channels tab's bookmark.
  if (page === "dashboard" && dashTabIsRetiredChannels(window.location.hash)) page = "channels";
  const retired = RETIRED_PAGE_REDIRECTS[page];
  if (retired) {
    if (retired.status) leadsSetPendingStatus(retired.status);
    if (retired.section) pipelineSetPendingSection(retired.section);
    if (retired.tab) stSetPendingTab(retired.tab);
    navigate(retired.page, options);
    return;
  }

  // Route alias: "ngrams" redirects to search-terms with Patterns tab active
  if (page === "ngrams") {
    navigate("search-terms", options);
    // Activate patterns tab after navigation
    setTimeout(() => {
      const patternsTab = document.getElementById("tab-btn-patterns");
      if (patternsTab) patternsTab.click();
    }, 0);
    return;
  }

  // Validate page exists — fallback to dashboard for invalid routes
  if (!PAGES.includes(page) || adminOnlyDenied(page)) {
    navigate("dashboard", { updateHash: true, replace: true });
    return;
  }

  // Role enforcement: health page is admin-only
  if (page === "health" && (!_currentUser || _currentUser.role !== "admin")) {
    navigate("dashboard", { updateHash: true, replace: true });
    return;
  }
  // Role enforcement: backfill page is admin-only
  if (page === "backfill" && (!_currentUser || _currentUser.role !== "admin")) {
    navigate("dashboard", { updateHash: true, replace: true });
    return;
  }
  // Role enforcement: revenue-health page is admin-only (PR-ADS-113)
  if (page === "revenue-health" && (!_currentUser || _currentUser.role !== "admin")) {
    navigate("dashboard", { updateHash: true, replace: true });
    return;
  }

  applyPageChrome(page);  // PR-ADS-124: chrome guard at navigation start

  // Close help drawer on page change
  closeHelpDrawer();

  // Hide all pages
  PAGES.forEach((p) => {
    const el = document.getElementById(`page-${p}`);
    if (el) el.hidden = true;
  });

  // Show target page
  const target = document.getElementById(`page-${page}`);
  if (target) target.hidden = false;

  // PR-ADS-110: Revenue & Attribution pages are clean business decision pages.
  // Suppress the global ad-window bar, the monitoring warning, the help button,
  // and the page-explanation block on these pages. Diagnostics live elsewhere
  // (System Status / Data Runs / Scheduler).
  // PR-ADS-110/124: revenue pages suppress the ad-window bar + monitoring banner
  // (also centralized in applyPageChrome; kept here too, idempotent).
  const revenuePage = isRevenuePage(page);

  const timeRangeBar = document.getElementById("time-range-bar");
  if (timeRangeBar) timeRangeBar.hidden = revenuePage;

  const monitoringBanner = document.getElementById("monitoring-banner");
  if (monitoringBanner && revenuePage) monitoringBanner.hidden = true;

  // Render page explanation panel (PR-ADS-070) — not on revenue pages.
  const explanationEl = document.getElementById(`page-explanation-${page}`);
  if (revenuePage) {
    if (explanationEl) { explanationEl.hidden = true; explanationEl.innerHTML = ""; }
  } else {
    renderPageExplanation(page);
    // Non-revenue navigation: re-evaluate the monitoring banner.
    loadMonitoringStatus();
  }

  // Update help drawer button state (PR-ADS-071) — hidden on revenue pages.
  const helpBtn = document.getElementById("page-help-btn");
  if (helpBtn) {
    const hasHelp = !revenuePage && !!PAGE_HELP_CONTENT[page];
    helpBtn.hidden = !hasHelp;
    helpBtn.dataset.page = page;
  }

  // Update active nav item: the menu item for any of its pages (PR-ADS-174A),
  // and the page's own tab in that item's strip.
  renderNavActive(page);

  _currentPage = page;

  // PR-ADS-124: re-assert chrome after _currentPage is set so any loader that
  // keys off _currentPage (e.g. loadMonitoringStatus) lands on the right state.
  applyPageChrome(page);

  // Update URL hash
  if (updateHash) {
    navigateToHash(page, { replace: options.replace });
  }

  loadPage(page);
}

function loadPage(page) {
  switch (page) {
    case "dashboard":     loadDashboardTab();                       break;
    case "campaigns":     loadCampaignsPage();                      break;
    case "search-terms":  loadSearchTermsEvidence();                break;
    case "pipeline":      loadPipeline();      break;
    case "gclid-attribution":
      if (gclidAttributionPrefill) {
        const campaignInput = document.getElementById("gclid-filter-campaign");
        if (campaignInput) campaignInput.value = gclidAttributionPrefill.campaign || "";
        gclidAttributionPrefill = null;
      }
      loadGclidAttribution({ reset: true });
      loadGclidCoverageForAttribution();
      loadGclidQuality();
      loadGclidReadiness();
      break;
    case "scheduler":     loadScheduler();     break;
    case "health":        loadHealth();        break;
    case "backfill":      loadBackfillStatus(); break;
    case "roas-countries":  loadRoasCountries();  break;
    case "revenue-health":  loadRevenueHealth();  break;
    case "channels":        loadChannels();        break;
    case "system":          loadSystem();          break;
  }
}

// ── Sidebar health dot ─────────────────────────────────────────────────────

async function loadSidebarHealth() {
  const dot  = document.getElementById("sidebar-status-dot");
  const text = document.getElementById("sidebar-status-text");
  try {
    const data = await fetch("/health").then((r) => r.json());
    if (data.status === "ok") {
      if (dot)  dot.className  = "status-dot status-dot--online";
      if (text) text.textContent = "Online";
    } else {
      if (dot)  dot.className  = "status-dot status-dot--error";
      if (text) text.textContent = "Degraded";
    }
  } catch (_) {
    if (dot)  dot.className  = "status-dot status-dot--error";
    if (text) text.textContent = "Offline";
  }
}

// ── Data freshness bar ─────────────────────────────────────────────────────

async function loadDataFreshness() {
  const barEl    = document.getElementById("data-freshness-bar");
  const statusEl = document.getElementById("freshness-status");
  if (!barEl || !statusEl) return;

  barEl.hidden = false;
  statusEl.textContent = "Checking data freshness…";
  statusEl.className   = "freshness-status";

  // Reset shared run metadata so per-page strips never show stale data from a
  // previous call if this one ends on an early-return path (DB offline, no run).
  _latestRunForMeta = null;

  // Freshness is global — always use a fixed 90d window, not the reporting filter.
  let latestRun     = null;
  let dbUnavailable = false;

  try {
    const data = await fetchJSON("/api/runs?days=90");
    if (data.db_unavailable) {
      dbUnavailable = true;
    } else {
      latestRun = (data.runs || [])[0] || null;  // already ordered DESC by started_at
    }
  } catch (_) { /* fetch failed entirely — will fall through to JSONL fallback */ }

  // If DB is unavailable or no DB run found, try the JSONL-backed /runs/latest fallback.
  if (!latestRun) {
    try {
      const fallback = await fetchJSON("/runs/latest");
      if (fallback && fallback.status !== "empty" && fallback.run_type) {
        latestRun = fallback;
      }
    } catch (_) { /* ignore — both sources unavailable */ }
  }

  // PR-ADS-124: the freshness bar lives in the same chrome row — re-assert the
  // guard after this async loader resolves so it never reappears on a revenue page.
  applyPageChrome(_currentPage);

  // Both sources unavailable — show explicit DB-offline error.
  if (dbUnavailable && !latestRun) {
    statusEl.textContent = "Run history unavailable · database offline";
    statusEl.className   = "freshness-status freshness-error";
    return;
  }

  // No run data available from any source.
  if (!latestRun) {
    statusEl.textContent = "No completed run found yet";
    statusEl.className   = "freshness-status freshness-empty";
    return;
  }

  // Store run metadata for use by per-page freshness strips.
  _latestRunForMeta = latestRun;

  const status     = normalizeRunStatus(latestRun);
  const runType    = runTypeLabel(latestRun.run_type);
  const timestamp  = latestRun.finished_at || latestRun.started_at;
  const dateStr    = fmtDate(timestamp);
  const ageDays    = timestamp
    ? Math.floor((Date.now() - new Date(timestamp).getTime()) / 86400000)
    : Infinity;

  if (status === "failed") {
    statusEl.textContent = `Latest recorded run failed · ${dateStr} · check Scheduler`;
    statusEl.className   = "freshness-status freshness-error";
  } else if (status === "running") {
    statusEl.textContent = `Latest run in progress · ${runType} · ${dateStr}`;
    statusEl.className   = "freshness-status freshness-warning";
  } else if (status === "partial") {
    // PR-ADS-160 (fourth review). A partial run used to fall through to the
    // green branch and read "Latest recorded run · <date> · partial" in the OK
    // style — fresh-looking, beside the word that says it is not. Real work
    // landed, so it is not an error; not everything landed, so it is not fresh.
    statusEl.textContent =
      `Latest run partial — some datasets were incomplete · ${dateStr} · ${runType} · check Scheduler`;
    statusEl.className   = "freshness-status freshness-warning";
  } else if (ageDays > _staleAfterDays) {
    statusEl.textContent = `Latest recorded run is stale · ${dateStr} · ${runType} · ${status}`;
    statusEl.className   = "freshness-status freshness-warning";
  } else {
    statusEl.textContent = `Latest recorded run · ${dateStr} · ${runType} · ${status}`;
    statusEl.className   = "freshness-status freshness-ok";
  }
}

// PR-ADS-181A: a recorded run type in plain words (api/monitoring.py
// RUN_TYPE_CADENCE and RETIRED_RUN_TYPES, and the jobs that write `runs`).
// A run type not listed here is shown AS ITSELF — never hidden, never renamed
// into a neighbour: an unregistered scheduler is something to see.
const RUN_TYPE_LABELS = {
  daily: "Daily check (morning)",
  daily_incremental_sync: "Daily data sync",
  monthly: "Monthly refresh",
  weekly: "Weekly refresh (retired)",
  backfill: "Backfill",
  revenue_recovery: "Revenue recovery",
};

function runTypeLabel(runType) {
  const key = runType === null || runType === undefined || runType === "" ? "" : String(runType);
  if (!key) return "unknown run";
  return Object.prototype.hasOwnProperty.call(RUN_TYPE_LABELS, key) ? RUN_TYPE_LABELS[key] : key;
}

// ── Monitoring status banner ────────────────────────────────────────────────

async function loadMonitoringStatus() {
  const bannerEl = document.getElementById("monitoring-banner");
  const textEl   = document.getElementById("monitoring-banner-text");
  if (!bannerEl || !textEl) return;

  // Hide by default; only show when warnings are present.
  bannerEl.hidden = true;
  bannerEl.className = "monitoring-banner";

  // PR-ADS-110/124: never on a revenue page (re-checked after the await too).
  const pageAtRequestStart = _currentPage;
  if (isRevenuePage(_currentPage)) return;
  if (isRevenuePage(pageAtRequestStart)) return;

  let data = null;
  try {
    data = await fetchJSON("/api/monitoring/status");
  } catch (_) {
    // Monitoring endpoint unavailable — fail silently; do not block the UI.
    return;
  }

  // PR-ADS-124: re-check the CURRENT page after the await — if we navigated onto
  // a revenue page mid-flight, never paint the banner (the race the spec calls out).
  if (isRevenuePage(_currentPage)) return;
  if (_currentPage !== pageAtRequestStart && isRevenuePage(_currentPage)) return;

  if (!data || data.severity === "green" || !data.warnings || data.warnings.length === 0) {
    return;
  }

  const isAdmin = _currentUser && _currentUser.role === "admin";
  const severity = data.severity || "yellow";

  const message = isAdmin
    ? `Monitoring warning: ${data.warnings.join(" ")} Check Scheduler / System Health.`
    : "Monitoring warning: Recent automated analysis may be stale. Contact your administrator.";

  textEl.textContent = message;
  bannerEl.className = `monitoring-banner monitoring-banner--${severity === "red" ? "red" : "yellow"}`;
  bannerEl.hidden = false;
}

// ── Per-page freshness strip helper ───────────────────────────────────────

// Render a compact freshness line beneath a page header using the latest run
// data fetched by loadDataFreshness().  sectionKey matches the data-section
// attribute on the placeholder <p id="run-meta-{sectionKey}">.
//
// Sections that depend on monthly data (campaigns, waste, keywords,
// geo, lead_quality, deals, gclid_attribution, search_terms,
// ngrams, in_progress_leads) use the latest run of any type because the global
// freshness bar already shows the latest run.  If no run data is available the
// strip shows a neutral unavailability note — it never shows false-fresh state.
function renderRunMeta(sectionKey) {
  const el = document.getElementById(`run-meta-${sectionKey}`);
  if (!el) return;

  if (!_latestRunForMeta) {
    el.textContent = "Freshness unavailable. Recent run information is not available. Refresh the page or contact your administrator if this persists.";
    el.className   = "run-meta";
    return;
  }

  const run       = _latestRunForMeta;
  const status    = normalizeRunStatus(run);
  const runType   = runTypeLabel(run.run_type);
  const timestamp = run.finished_at || run.started_at;
  const dateStr   = fmtDate(timestamp);
  const ageDays   = timestamp
    ? Math.floor((Date.now() - new Date(timestamp).getTime()) / 86400000)
    : Infinity;

  const isStale = ageDays > _staleAfterDays || status === "failed";

  if (status === "failed") {
    el.textContent = `Data source: latest ${runType} analysis · Finished: ${dateStr} · Last run failed — check Scheduler`;
    el.className   = "run-meta is-stale";
  } else if (status === "running") {
    el.textContent = `Data source: latest ${runType} analysis · Run in progress · ${dateStr}`;
    el.className   = "run-meta";
  } else if (status === "partial") {
    // Never "Fresh": the run that produced this page's data did not finish
    // what it set out to do, so the page is showing an incomplete population.
    el.textContent = `Data source: latest ${runType} analysis · Finished: ${dateStr} · Latest run partial — some datasets were incomplete`;
    el.className   = "run-meta is-stale";
  } else if (isStale) {
    el.textContent = `Data source: latest ${runType} analysis · Finished: ${dateStr} · Stale`;
    el.className   = "run-meta is-stale";
  } else {
    el.textContent = `Data source: latest ${runType} analysis · Finished: ${dateStr} · Fresh`;
    el.className   = "run-meta is-fresh";
  }
}

// ── Per-page dataset-level freshness strip (PR-ADS-074) ───────────────────
//
// Renders a compact dataset-specific freshness line into the run-meta-{sectionKey}
// placeholder for the given page.  Uses _datasetFreshnessByKey (populated by
// loadDatasetFreshness()) and PAGE_DATASET_MAP to show the exact dataset(s) that
// power each page rather than the generic latest-run timestamp.
//
// Falls back to renderRunMeta() when the cache is not yet populated so that
// pages always show something even on first load.
//
// Status labels are intentionally consistent with the System Health dataset
// freshness table: Fresh / Stale / Failed / Running / Unknown.

function _sourceDisplayName(source) {
  // PR-ADS-105: Google Ads API is the active platform-evidence source.
  // Windsor remains only as a legacy/deprecated source label.
  const names = {
    google_ads_api: "Google Ads API",
    meta_ads_api: "Meta Ads API",
    hubspot: "HubSpot",
    gclid: "GCLID Attribution",
    windsor: "Windsor legacy",
  };
  return names[source] || escapeHtml(source);
}

function _datasetDisplayName(key) {
  const labels = {
    "google_ads_api/canonical_spend": "Campaign spend data",
    "google_ads_api/search_terms": "Search-term data",
    "google_ads_api/keyword_facts": "Keyword evidence data",
    "google_ads_api/keywords":     "Keyword data (legacy snapshot)",
    "google_ads_api/canonical_geo": "Country spend data (daily)",
    "google_ads_api/geo":          "Geo data (legacy monthly snapshot)",
    "meta_ads_api/meta_campaign_spend": "Meta Ads spend data",
    "meta_ads_api/meta_adset_spend": "Meta Ads ad-set data",
    "hubspot/contacts":     "Contacts data",
    "hubspot/deals":        "Deals data",
    "gclid/matches":        "Attribution data",
  };
  return labels[key] || escapeHtml(key);
}

// Returns the human-readable "Derived from …" prefix for derived pages.
function _derivedPageLabel(sectionKey) {
  const labels = {
    ngrams: "Derived from search terms",
  };
  return labels[sectionKey] || "Derived dataset";
}

function renderPageDatasetFreshness(sectionKey) {
  const el = document.getElementById(`run-meta-${sectionKey}`);
  if (!el) return;

  const datasetKeys = PAGE_DATASET_MAP[sectionKey];
  // No mapping → fall back to run-based strip (e.g. scheduler, health).
  if (!datasetKeys) {
    renderRunMeta(sectionKey);
    return;
  }

  // Cache not yet populated → fall back to run-based strip.  After
  // loadDatasetFreshness() resolves it calls renderPageDatasetFreshness for
  // the visible page, so this is only ever a brief fallback.
  if (Object.keys(_datasetFreshnessByKey).length === 0) {
    renderRunMeta(sectionKey);
    return;
  }

  const isDerived = DERIVED_DATASET_PAGES.has(sectionKey);

  // ── Single-dataset pages ──────────────────────────────────────────────────
  if (datasetKeys.length === 1) {
    const key           = datasetKeys[0];
    const row           = _datasetFreshnessByKey[key];
    const dsName        = _datasetDisplayName(key);

    // Use canonical_status from backend (PR-ADS-067) when available
    const cs = row ? row.canonical_status : null;

    if (!row || !cs || cs === "unknown") {
      el.textContent = `${isDerived ? _derivedPageLabel(sectionKey) : `Dataset freshness: ${dsName}`} · Unknown (freshness unverified)`;
      el.className   = "run-meta";
      return;
    }

    const _csLabels = {
      fresh_with_data:                   "Fresh with data",
      fresh_but_empty:                   "Fresh but empty",
      stale_with_data:                   "Stale with data",
      stale_and_empty:                   "Stale and empty",
      failed:                            "Failed",
      running:                           "Running",
      not_run:                           "Not run",
      dependency_blocked:                "Blocked by dependency",
      db_unavailable:                    "DB unavailable",
      // PR-ADS-095 refined states
      data_available_latest_sync_failed: "Data available, latest sync failed",
      failed_no_data:                    "Failed, no data",
      // PR-ADS-160: a sync that did real work and stopped short.
      data_available_latest_sync_partial: "Data available, latest sync partial",
      partial_no_data:                   "Partial sync, no data",
      not_run_but_derivable:             "Not run, but derivable",
      not_run_no_upstream_data:          "Not run, no upstream data",
      unknown_row_count:                 "Row count unavailable",
      row_count_not_enabled:             "Row count not enabled",
      blocked_by_dependency:             "Blocked by dependency",
      empty_success:                     "Empty success",
    };
    const _csClasses = {
      fresh_with_data:                   "run-meta is-fresh",
      fresh_but_empty:                   "run-meta is-canonical-warning",
      stale_with_data:                   "run-meta is-stale",
      stale_and_empty:                   "run-meta is-stale",
      failed:                            "run-meta is-stale",
      running:                           "run-meta",
      not_run:                           "run-meta",
      dependency_blocked:                "run-meta is-canonical-warning",
      db_unavailable:                    "run-meta is-stale",
      // PR-ADS-095 refined states
      data_available_latest_sync_failed: "run-meta is-canonical-warning",
      failed_no_data:                    "run-meta is-stale",
      // Never `is-fresh`: the rows shown come from an incomplete population.
      data_available_latest_sync_partial: "run-meta is-canonical-warning",
      partial_no_data:                   "run-meta is-stale",
      not_run_but_derivable:             "run-meta is-canonical-warning",
      not_run_no_upstream_data:          "run-meta is-stale",
      unknown_row_count:                 "run-meta",
      row_count_not_enabled:             "run-meta",
      blocked_by_dependency:             "run-meta is-stale",
      empty_success:                     "run-meta is-canonical-warning",
    };

    const prefix = isDerived ? _derivedPageLabel(sectionKey) : `Dataset freshness: ${dsName}`;
    const label = _csLabels[cs] || cs;
    const reason = row.reason ? ` — ${row.reason}` : "";
    el.textContent = `${prefix} · ${label}${reason}`;
    el.className = _csClasses[cs] || "run-meta";
    return;
  }

  // ── Multi-dataset pages ───────────────────────────────────────────────────
  const statuses = datasetKeys.map((key) => {
    const row = _datasetFreshnessByKey[key];
    return row ? (row.canonical_status || datasetDisplayStatus(row)) : "unknown";
  });

  const freshCount   = statuses.filter((s) => s === "fresh_with_data" || s === "success").length;
  const warningCount = statuses.filter((s) =>
    s === "fresh_but_empty" || s === "stale_with_data" || s === "dependency_blocked"
    || s === "data_available_latest_sync_failed" || s === "not_run_but_derivable"
    || s === "empty_success" || s === "data_available_latest_sync_partial"
    || s === "partial" || s === "stale"
  ).length;
  const errorCount   = statuses.filter((s) =>
    s === "failed" || s === "stale_and_empty" || s === "db_unavailable"
    || s === "failed_no_data" || s === "not_run_no_upstream_data" || s === "blocked_by_dependency"
    || s === "partial_no_data"
  ).length;
  const runningCount = statuses.filter((s) => s === "running").length;
  const unknownCount = statuses.filter((s) =>
    s === "unknown" || s === "not_run" || s === "row_count_not_enabled" || s === "unknown_row_count"
  ).length;
  const totalCount   = datasetKeys.length;

  const detail = datasetKeys.map((key, i) => {
    const _shortLabels = {
      fresh_with_data: "Fresh", fresh_but_empty: "Empty", stale_with_data: "Stale",
      stale_and_empty: "Stale+Empty", failed: "Failed", running: "Running",
      not_run: "Not run", dependency_blocked: "Blocked", db_unavailable: "DB down",
      unknown: "Unknown", success: "Fresh", stale: "Stale",
      // PR-ADS-181A: datasetDisplayStatus passes a row's own run status
      // through when it has no canonical status — a partial sync among them.
      // Without this key it read "Unknown" beside a tally that missed it.
      partial: "Partial",
      // PR-ADS-095 refined states
      data_available_latest_sync_failed: "Degraded",
      failed_no_data:                    "Failed",
      // PR-ADS-160: without these two, a multi-dataset strip renders a correct
      // warning/error SUMMARY beside a detail reading "Contact funnel: Unknown"
      // — the tally and the per-dataset label disagreeing in the same line,
      // with the label the more specific and more wrong of the two.
      data_available_latest_sync_partial: "Partial",
      partial_no_data:                   "Partial, no data",
      not_run_but_derivable:             "Derivable",
      not_run_no_upstream_data:          "No upstream",
      unknown_row_count:                 "Row count?",
      row_count_not_enabled:             "Counts off",
      blocked_by_dependency:             "Blocked",
      empty_success:                     "Empty (ok)",
    };
    return `${_datasetDisplayName(key)}: ${_shortLabels[statuses[i]] || "Unknown"}`;
  }).join(" · ");

  const sourceWord = totalCount === 1 ? "source" : "sources";
  const summaryParts = [`Dataset freshness: ${totalCount} ${sourceWord}`];
  if (freshCount > 0) summaryParts.push(`${freshCount} fresh`);
  if (warningCount > 0) summaryParts.push(`${warningCount} warning`);
  if (errorCount > 0) summaryParts.push(`${errorCount} error`);
  if (runningCount > 0) summaryParts.push(`${runningCount} running`);
  if (unknownCount > 0) summaryParts.push(`${unknownCount} unknown`);
  const summary = summaryParts.join(" · ");

  el.textContent = `${summary} · ${detail}`;
  el.className = (errorCount > 0)
    ? "run-meta is-stale"
    : (warningCount > 0 ? "run-meta is-canonical-warning" : (freshCount === totalCount ? "run-meta is-fresh" : "run-meta"));
}

// ── Dashboard — Executive Overview command center (PR-ADS-134) ─────────────
//
// The old ad-window KPI dashboard is gone. The dashboard is now an executive
// business-truth page: canonical KPIs, revenue-vs-spend trend, pipeline funnel,
// channel mix, computed decision cards and ranked signals — all read from ONE
// read-only contract (/api/dashboard/overview) composed from the canonical
// Revenue Decision Mart. Truth rules are never loosened in the renderer:
//   - Unavailable metrics render as "Unavailable", never a fabricated $0/0.00x.
//   - Native GBP spend is never rendered with a "$".
//   - ROAS renders for Google Ads only, and only when the API says it is safe.
//   - A missing previous period renders "No comparison", never 0%.

let dashOverview = null;
let dashOverviewStatus = "loading";

// Chart palette — brand hues snapped to the dataviz lightness/CVD checks
// (validated: revenue bar #129ef5 + spend line #0a5f92 pass adjacent CVD;
// the muted Offline slot is a deliberate de-emphasis, relieved by the labeled
// legend list which carries every value as text).
const DASH_SERIES_COLORS = { revenue: "#129ef5", spend: "#0a5f92" };
const DASH_DONUT_COLORS = {
  google_ads: "#129ef5",
  other_paid: "#0a5f92",
  organic: "#15803d",
  offline: "#94a3b8",
  unclassified: "#b45309",
};

// Metric valence for delta chips: up is good for revenue/pipeline, neutral for
// spend (more spend is neither win nor loss without revenue proof).
const DASH_DELTA_VALENCE = {
  spend_usd: "neutral",
  revenue_usd: "up-good",
  sqls: "up-good",
  customers: "up-good",
  closed_won_deals: "up-good",
  period_revenue_per_ad_spend: "up-good",
  // PR-ADS-135 revenue-tab metric keys.
  closed_won_revenue_usd: "up-good",
  average_deal_value_usd: "up-good",
  // PR-ADS-136 channels-tab metric keys.
  total_closed_won_deals: "up-good",
  total_sqls: "up-good",
  // PR-ADS-137 campaigns-tab metric keys.
  won_revenue_usd: "up-good",
  verified_spend_usd: "neutral",
  // PR-ADS-138 countries-tab metric key.
  geo_period_revenue_per_ad_spend: "up-good",
  // PR-ADS-139 deals-tab metric keys.
  closed_won_customers: "up-good",
  sql_to_customer_rate: "up-good",
};

// ── Dashboard inner-tab controller (PR-ADS-135) ───────────────────────────
// Overview is the default; Revenue is linkable via #/dashboard?tab=revenue.
// Both roots reuse the same PR-134 design system — no new visual language.

// PR-ADS-181A: the retired Dashboard → Channels tab. A bookmark of it opens the
// Channels page (the one channel surface) instead of a silent Overview.
function dashTabIsRetiredChannels(hash) {
  const q = new URLSearchParams(String(hash || "").split("?")[1] || "");
  return q.get("tab") === "channels";
}

function loadDashboardTab() {
  const tab = hashToDashTab(window.location.hash);
  activateDashboardTab(tab, { updateHash: false });
}

function activateDashboardTab(tab, options) {
  options = options || {};
  const target = DASHBOARD_TABS.includes(tab) ? tab : "overview";

  document.querySelectorAll(".dashboard-tab[data-dash-tab]").forEach((btn) => {
    const active = btn.dataset.dashTab === target;
    btn.classList.toggle("is-active", active);
    if (active) btn.setAttribute("aria-current", "page");
    else btn.removeAttribute("aria-current");
  });

  const overviewRoot = document.getElementById("dashboard-overview-root");
  const revenueRoot = document.getElementById("dashboard-revenue-root");
  const campaignsRoot = document.getElementById("dashboard-campaigns-root");
  const countriesRoot = document.getElementById("dashboard-countries-root");
  const dealsRoot = document.getElementById("dashboard-deals-root");
  if (overviewRoot) overviewRoot.hidden = target !== "overview";
  if (revenueRoot) revenueRoot.hidden = target !== "revenue";
  if (campaignsRoot) campaignsRoot.hidden = target !== "campaigns";
  if (countriesRoot) countriesRoot.hidden = target !== "countries";
  if (dealsRoot) dealsRoot.hidden = target !== "deals";

  if (options.updateHash !== false) {
    // Overview is the default → keep the URL clean; other tabs are bookmarkable.
    const hash = target === "overview" ? "#/dashboard" : `#/dashboard?tab=${target}`;
    if (window.location.hash !== hash) history.replaceState(null, "", hash);
  }

  if (target === "revenue") loadDashboardRevenue();
  else if (target === "campaigns") loadDashboardCampaigns();
  else if (target === "countries") loadDashboardCountries();
  else if (target === "deals") loadDashboardDeals();
  else loadDashboardOverview();
}

async function loadDashboardOverview() {
  const window_ = getRoasBusinessWindow();
  const token = ++_revReqSeq.overview;
  setWindowRangeLoading("dashboard-range");

  const root = document.getElementById("dashboard-overview-root");
  const genEl = document.getElementById("dashboard-generated");
  if (root) {
    if (dashOverview && dashOverviewStatus === "ok") {
      // Refetch keeps the frame: hold the previous render, dimmed — no
      // skeleton flash, no layout jump.
      root.classList.add("is-refreshing");
    } else {
      root.innerHTML = renderDashSkeleton();
    }
  }

  try {
    const data = await fetchJSON(
      `/api/dashboard/overview?window=${encodeURIComponent(window_)}`
    );
    if (token !== _revReqSeq.overview) return; // stale response superseded
    if (!_revResponseIsCurrent("overview", token, data)) return;
    dashOverview = data;
    dashOverviewStatus = "ok";
    renderWindowRange("dashboard-range", data.window || null);
    if (genEl) {
      genEl.textContent = data.generated_at ? `Generated ${fmtDate(data.generated_at)}` : "";
    }
    renderDashboardOverview();
  } catch (err) {
    console.error("[loadDashboardOverview]", err);
    if (token !== _revReqSeq.overview) return;
    dashOverview = null;
    dashOverviewStatus = "error";
    if (genEl) genEl.textContent = "";
    renderWindowRange("dashboard-range", null); // never leave "Loading…" stuck
    renderDashboardOverview();
  }
}

function renderDashSkeleton() {
  const card = '<div class="dash-skeleton__card"></div>';
  return `
    <div class="dash-skeleton" aria-hidden="true">
      <div class="dash-skeleton__row">${card.repeat(5)}</div>
      <div class="dash-skeleton__wide"></div>
      <div class="dash-skeleton__row">${card.repeat(4)}</div>
    </div>
    <p class="empty-state">Loading executive overview…</p>`;
}

function renderDashboardOverview() {
  const root = document.getElementById("dashboard-overview-root");
  if (!root) return;
  root.classList.remove("is-refreshing");

  if (dashOverviewStatus !== "ok" || !dashOverview) {
    const nextStep = dashCanNavigate("health")
      ? 'Next step: check <a href="#/health">System Status</a> or retry with Refresh.'
      : "Next step: retry with Refresh, or contact your administrator.";
    root.innerHTML = `
      <div class="revenue-blocked-card dash-error-card">
        <h3 class="revenue-blocked-card__title">Executive overview unavailable</h3>
        <p>The dashboard overview service could not be reached. Nothing is shown
        because nothing safe could be computed — metrics are never fabricated.</p>
        <p class="revenue-blocked-card__next">${nextStep}</p>
      </div>`;
    return;
  }

  const d = dashOverview;
  root.innerHTML = `
    ${renderDashTruthStrip(d)}
    ${renderDashKpiRow(d)}
    <div class="dash-main-grid">
      <section class="dash-panel dash-chart-panel dash-anim" aria-label="Revenue vs spend trend">
        ${renderDashTrendPanel(d)}
      </section>
      <aside class="dash-signal-rail dash-anim" aria-label="Top signals">
        ${renderDashSignals(d)}
      </aside>
    </div>
    ${renderDashLifecycleActivity(d)}
    ${renderDashAcquisitionFunnel(d)}
    ${renderDashCommercialOutcomes(d)}
    <div class="dash-lower-grid">
      <section class="dash-panel dash-anim" aria-label="Channel mix">
        ${renderDashSourceMix(d)}
      </section>
      <section class="dash-decisions dash-anim" aria-label="Decision cards">
        ${renderDashDecisionCards(d)}
      </section>
    </div>
    ${renderDashTruthFooter(d)}
  `;
  wireDashChartHover(root, d);
}

// One value cell that is NEVER a fake zero: null renders as "Unavailable".
function dashValue(v, formatter) {
  if (v === null || v === undefined) {
    return '<span class="dash-unavailable">Unavailable</span>';
  }
  return escapeHtml(formatter ? formatter(v) : String(v));
}

// ── PR-ADS-161E — canonical closed-won metrics ───────────────────────────────
// Closed-won deals, customers, revenue, ROAS and CAC arrive from the canonical
// service as {value, status, reason}. A published zero renders "0"; withheld
// and unavailable render the word and the canonical reason — never a zero, a
// dash, a stale value, or the number with a suffix. Nothing here decides a
// verdict; it only says what the API decided.
const CANONICAL_REASON_LABELS = {
  customer_identity_unresolved: "a won deal has no company, or several",
  customer_identity_evidence_stale: "a won deal's company evidence is stale",
  customer_identity_evidence_unavailable: "a won deal's company was never observed",
  customer_identity_scan_not_current: "the company association scan is not current",
  company_associations_unreadable: "company associations could not be read",
  company_associations_not_ingested: "company associations are not ingested",
  closed_won_deals_missing_proven_usd_amount: "a won deal has no proven USD amount",
  closed_won_deals_missing_amount: "a won deal has no proven USD amount",
  won_deals_without_close_date: "a won deal has no close date",
  won_flag_and_stage_disagree: "HubSpot's won flag and won stage disagree",
  google_ads_deals_not_placed_on_a_campaign: "a Google Ads won deal has no campaign",
  closed_won_deal_count_withheld: "the closed-won deal count is withheld",
  closed_won_revenue_withheld: "closed-won revenue is withheld",
  customer_count_withheld: "the customer count is withheld",
  google_ads_usd_spend_unavailable: "Google Ads USD spend is unavailable",
  google_ads_spend_coverage_or_fx_unverified: "Google Ads spend coverage or FX is unverified",
  campaign_spend_mapping_unavailable: "this campaign has no mapped Google Ads spend",
  no_google_ads_spend_in_window: "no Google Ads spend in this window",
  no_customers_in_window: "no customers in this window",
  canonical_coverage_not_proven: "deal-ledger coverage is not proven",
  canonical_ledger_stale: "the deal sync is overdue, so these figures may be out of date",
  canonical_ledger_unreadable: "the deal ledger could not be read",
  canonical_sync_state_unreadable: "the deal sync state could not be read",
  legacy_local_revenue_not_canonical: "computed from local data outside the canonical closed-won ledger",
  acquisition_membership_unresolved: "a won deal's acquisition contact is unresolved",
  campaign_identity_unavailable: "campaign identity is unavailable",
  canonical_closed_won_population_unavailable: "the closed-won population is unavailable",
  // PR-ADS-178C: the four PR-ADS-166A "Customers won" refusal reasons
  // (customers_won_needs_a_business_window / _not_split_by_contact_source /
  // _unreadable / _window_mismatch) are deleted: only the retired route and
  // the retired Leads page sent them. The Pipeline builder refuses a
  // non-business window with a 400, and no lead filter touches the list.
  // PR-ADS-164A: the two fail-closed reasons a page gives when it cannot use
  // the verdict it was sent (see closedWonBlockedMetric).
  closed_won_verdict_missing: "the server sent no usable publication verdict for these deals",
  closed_won_rows_do_not_match_count: "the deal rows received do not match the published count",
  // PR-ADS-178B-F1: the Pipeline page's own reasons, in plain words.
  no_closed_won_deals_in_window: "no closed-won deals in this window, so there is no average",
  average_deal_value_population_mismatch:
    "the average was computed over a different deal count or revenue than the figures beside it",
  missing_stage_entry_date: "some contacts reached a stage with no entry date recorded in HubSpot",
  unknown_lifecycle_stage: "some contacts hold a lifecycle stage this funnel does not recognise",
  sql_to_customer_rate_needs_one_acquisition_cohort_and_scope:
    "SQLs and won deals are different populations and cohorts",
  revenue_per_sql_needs_one_acquisition_cohort_and_scope:
    "revenue and SQLs are different populations and cohorts",
  lead_to_customer_rate_needs_one_acquisition_cohort_and_scope:
    "leads and won deals are different populations and cohorts",
  // PR-ADS-168A: the Channels page's outcome and spend columns.
  channel_spend_not_connected: "no spend source is connected for this channel",
  channel_population_is_not_spend_population:
    "this channel's deals are not the population Google Ads spend paid for — see the Google Ads line",
  spend_shown_on_google_ads_line: "Google Ads spend is shown on the Google Ads line, beside the deals it is compared with",
  account_time_zone_differs_from_evidence_days:
    "the Google Ads account time zone differs from the days these deals are counted on",
  google_ads_evidence_partition_unavailable: "the Google Ads evidence partition is unavailable",
  google_ads_evidence_does_not_reconcile: "the Google Ads evidence deals do not match the period's partition",
  all_channel_spend_not_connected: "no spend source covers every channel",
  deal_source_not_resolved: "these deals have no single source, so no spend applies",
  channel_partition_does_not_reconcile: "the channel rows do not add up to the period's won deals",
  channel_outcomes_failed: "the channel outcomes could not be loaded",
  spend_not_reread_since_day_ended: "yesterday's spend has not been re-read since the day ended",
  spend_reread_failed: "the sync that should have re-read yesterday's spend failed",
  window_includes_day_in_progress: "the window includes a day still in progress",
  account_time_zone_unknown: "the Google Ads account time zone is not on record",
  spend_days_not_settled: "the spend days are not settled yet",
  no_settled_day_in_window: "this window has no completed day yet",
  // PR-ADS-171B: the Meta Ads (spend) line and the Paid Social row beside it.
  spend_shown_on_meta_ads_line: "Meta Ads spend is shown on its own line, not beside HubSpot's Paid Social deals",
  channel_population_is_not_meta_spend_population:
    "HubSpot's Paid Social contacts and deals are not proven to be the population Meta spend paid for",
  meta_ads_not_connected: "Meta Ads is not set up",
  meta_ads_spend_source_unavailable: "stored Meta spend could not be read, or Meta is set up incorrectly",
  meta_ads_spend_coverage_incomplete: "some ad account's spend is not verified and settled for every day",
  fx_rates_missing: "a day with Meta spend has no USD exchange rate",
  fx_currency_not_supported: "an ad account's currency has no USD exchange rate source",
  meta_accounts_in_different_currencies: "the ad accounts use different currencies, so there is no single native total",
  meta_retired_disclosure_missing: "the figure rests on the owner's retirement declaration and its disclosure is missing",
  meta_ads_spend_line_failed: "the Meta spend line could not be loaded",
  roas_requires_revenue_and_spend_in_one_acquisition_cohort: "attribution ROAS needs revenue and spend over one acquisition cohort",
  cac_requires_spend_and_customers_in_one_acquisition_cohort: "CAC needs spend and customers over one acquisition cohort",
};

// PR-ADS-161E decision 1: a close-date ratio is NOT ROAS or CAC. In a long B2B
// sales cycle a deal closed this window was usually acquired by earlier spend,
// so revenue (or customers) closed in a window over that window's spend is a
// non-cohort operational ratio. It is shown under these explicit labels, always
// with the disclosure, and the API never carries it in a field named roas/cac.
const PERIOD_RATIO_LABEL = "Period Revenue / Ad Spend";
const PERIOD_SPEND_PER_CUSTOMER_LABEL = "Spend per Customer Won This Period";
const PERIOD_RATIO_DISCLOSURE =
  "Non-cohort operational ratio · Closed-won outcomes may originate from earlier advertising periods";
// Certified attribution metrics: always Unavailable until spend and outcomes
// are joined over one acquisition cohort and attribution scope.
const CERTIFIED_RATIO_LABELS = { roas: "Attribution ROAS (acquisition cohort)", cac: "CAC (acquisition cohort)" };

const CANONICAL_BASIS_LABELS = {
  close_date_outcomes_over_same_window_spend: PERIOD_RATIO_DISCLOSURE,
};

function canonicalReasonLabel(reason) {
  if (!reason) return "";
  return CANONICAL_REASON_LABELS[reason] || countryReasonLabel(reason)
    || String(reason).replace(/_/g, " ");
}

// The CRM Truth Exceptions family a withholding reason points at. Only CRM
// findings have one: an outage or an unproven sync has no record to fix.
const CRM_BLOCKER_FAMILY = {
  customer_identity_unresolved: "customer_identity",
  customer_identity_evidence_stale: "customer_identity",
  customer_identity_evidence_unavailable: "customer_identity",
  customer_identity_scan_not_current: "customer_identity",
  company_associations_unreadable: "customer_identity",
  closed_won_deals_missing_proven_usd_amount: "revenue_proof",
  closed_won_deals_missing_amount: "revenue_proof",
  won_deals_without_close_date: "won_deal_membership",
  won_flag_and_stage_disagree: "won_deal_membership",
};

function crmBlockerFamily(reason) {
  return CRM_BLOCKER_FAMILY[reason] || null;
}

// "View blockers" — ADMIN ONLY. The workbench and its API are admin-gated, so a
// viewer gets the truthful status and reason but never this link.
function crmBlockersHref(opts) {
  const o = opts || {};
  if (!(_currentUser && _currentUser.role === "admin")) return null;
  const family = o.family || crmBlockerFamily(o.reason);
  if (!family) return null;
  const p = new URLSearchParams();
  if (o.windowType && o.windowKey) p.set("cte_window", `${o.windowType}/${o.windowKey}`);
  p.set("cte_family", family);
  return `#/health?${p.toString()}`;
}

// The input a derived metric (a period ratio) is blocked by names the CRM family.
function canonicalBlockingReason(metric) {
  const m = metric || {};
  return (m.blocked_by && m.blocked_by.reason) || m.reason || null;
}

function canonicalMetricHtml(metric, formatter, opts) {
  const m = metric || {};
  if (m.status === "published" && m.value !== null && m.value !== undefined) {
    const text = escapeHtml(formatter ? formatter(m.value) : String(m.value));
    // A derived ratio states its basis (PR-ADS-161E): the period ratios are
    // close-date outcomes over the same window's spend, not an acquisition
    // cohort — the disclosure is always shown beside the value.
    return m.cohort_basis
      ? `<span title="${escapeHtml(CANONICAL_BASIS_LABELS[m.cohort_basis] || m.cohort_basis)}">${text}</span>`
        + ` <small class="metric-reason">${escapeHtml(CANONICAL_BASIS_LABELS[m.cohort_basis] || m.cohort_basis)}</small>`
      : text;
  }
  const word = m.status === "withheld" ? "Withheld" : "Unavailable";
  const reason = canonicalBlockingReason(m);
  const label = canonicalReasonLabel(reason);
  const href = crmBlockersHref({ ...(opts || {}), reason });
  return `<span class="dash-unavailable" title="${escapeHtml(label)}">${word}</span>`
    + (label ? ` <small class="metric-reason">${escapeHtml(label)}</small>` : "")
    + (href ? ` <a class="metric-blockers-link" href="${escapeHtml(href)}">View blockers</a>` : "");
}

// A per-row closed-won metric (a campaign, a country, a source group). When the
// API sent the row's canonical {value, status, reason}, render that — a
// withheld subset reads "Withheld" and why, never a dash — else the plain value.
const ROW_METRIC_KEYS = { won_revenue_usd: "revenue_usd", won_revenue: "revenue_usd",
  closed_won_revenue_usd: "revenue_usd", closed_won_customers: "customers",
  deals: "closed_won_deals" };
function rowMetricHtml(row, key, formatter, opts) {
  const r = row || {};
  const m = (r.metrics || {})[ROW_METRIC_KEYS[key] || key];
  if (m && m.status) return canonicalMetricHtml(m, formatter, opts);
  return dashValue(r[key], formatter);
}

// PR-ADS-161E: a page total that is a number when published and, when not,
// says WHY with the canonical metric — never a bare "Unavailable" for a CRM
// withholding. Falls back to dashValue when the API sent no verdict.
function canonicalValueHtml(value, metric, formatter, opts) {
  if (value !== null && value !== undefined) {
    return escapeHtml(formatter ? formatter(value) : String(value));
  }
  const m = metric || {};
  if (m.status && m.status !== "published") return canonicalMetricHtml(m, formatter, opts);
  return dashValue(null);
}

// ── PR-ADS-164A — a won-deal list is read, empty, or not published ───────────
// The Deals page lists a closed-won population (Revenue by Source and its
// drawer did too until PR-ADS-168B deleted them). The API sends that population's canonical
// verdict as `closed_won_population` = {value, status, reason}. The verdict
// decides what an empty list means; an empty array alone never does:
//   "rows"    published, and exactly that many deals arrived;
//   "empty"   published with value 0 — the ledger WAS read and holds no deal;
//   "blocked" everything else: withheld, unavailable, an unrecognised status,
//             no verdict at all, or a published count the rows contradict.
// `dealCount` is the number of deals the page received: its rows.
function closedWonPopulation(data) {
  return (data && data.closed_won_population) || null;
}

function closedWonPopulationPublished(verdict) {
  const v = verdict || {};
  return v.status === "published" && typeof v.value === "number";
}

function closedWonPopulationState(verdict, dealCount) {
  if (!closedWonPopulationPublished(verdict)) return "blocked";
  if (dealCount !== verdict.value) return "blocked";
  return verdict.value === 0 ? "empty" : "rows";
}

// What a blocked list says: the API's own word and reason. Where it gave no
// reason — no verdict, or a status this page does not know — the page says
// that, rather than nothing. Never the count, and never "none found".
function closedWonBlockedMetric(verdict) {
  const v = verdict || {};
  if (v.status === "published") {
    return { status: "unavailable", reason: "closed_won_rows_do_not_match_count" };
  }
  return { status: v.status === "withheld" ? "withheld" : "unavailable",
           reason: v.reason || "closed_won_verdict_missing", blocked_by: v.blocked_by };
}

function closedWonBlockedHtml(verdict) {
  return canonicalMetricHtml(closedWonBlockedMetric(verdict), null, businessBlockerOpts());
}

// Plain-text deal-count sub-line (escaped by the KPI template): the count, or
// the canonical word and reason.
function closedWonDealsSubText(value, metric) {
  if (value !== null && value !== undefined) return `${fmtCount(value)} closed-won deals`;
  const m = metric || {};
  const word = m.status === "withheld" ? "withheld" : "unavailable";
  const why = canonicalReasonLabel(canonicalBlockingReason(m));
  return `Closed-won deals ${word}${why ? ` — ${why}` : ""}`;
}

// Admin-gated routes never render as live links for viewers — navigate() would
// silently bounce them back to the dashboard, which reads as a broken link.
const DASH_ADMIN_PAGES = ["revenue-health", "health", "backfill"];

function dashCanNavigate(page) {
  if (!DASH_ADMIN_PAGES.includes(page)) return true;
  return !!(_currentUser && _currentUser.role === "admin");
}

// Delta chip for one period_change metric. A missing comparison is stated,
// never rendered as 0%.
function dashDeltaChip(metricKey, periodChange) {
  const metrics = (periodChange && periodChange.metrics) || {};
  const m = metrics[metricKey];
  const label = (periodChange && periodChange.label) || "vs previous period";
  if (!m || m.status !== "ok" || m.delta_pct === null || m.delta_pct === undefined) {
    return '<span class="dash-delta dash-delta--none">No comparison</span>';
  }
  const pct = m.delta_pct * 100;
  const magnitude = Math.abs(pct) >= 100 ? 0 : 1;
  const text = `${pct > 0 ? "+" : ""}${pct.toFixed(magnitude)}%`;
  const valence = DASH_DELTA_VALENCE[metricKey] || "neutral";
  let cls = "dash-delta--flat";
  if (m.direction === "up") {
    cls = valence === "up-good" ? "dash-delta--good" : "dash-delta--neutral";
  } else if (m.direction === "down") {
    cls = valence === "up-good" ? "dash-delta--bad" : "dash-delta--neutral";
  }
  const arrow = m.direction === "up" ? "▲" : (m.direction === "down" ? "▼" : "•");
  return `<span class="dash-delta ${cls}" title="${escapeHtml(label)}">` +
    `<span aria-hidden="true">${arrow}</span> ${escapeHtml(text)}</span>`;
}

// Series values for sparklines — null unless the API says the series is ready.
function dashTrendSeries(d, key) {
  const trend = d.trend || {};
  const statusKey = key === "spend_usd" ? "spend_status" : "revenue_status";
  if (trend[statusKey] !== "ready") return null;
  const vals = (trend.points || []).map((p) => p[key]);
  if (vals.length < 2 || vals.some((v) => v === null || v === undefined)) return null;
  return vals;
}

// 12-point-style sparkline: de-emphasis stroke, accent dot on the last point.
function dashSparkline(values) {
  if (!values || values.length < 2) return "";
  const W = 120, H = 30, pad = 3;
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const range = max - min || 1;
  const x = (i) => pad + (W - pad * 2) * (i / (values.length - 1));
  const y = (v) => pad + (H - pad * 2) * (1 - (v - min) / range);
  const pts = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const lastX = x(values.length - 1).toFixed(1);
  const lastY = y(values[values.length - 1]).toFixed(1);
  return `<svg class="dash-spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
    <polyline points="${pts}" fill="none" stroke="#cbd5e1" stroke-width="1.6"
      stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="${lastX}" cy="${lastY}" r="2.6" fill="#129ef5" stroke="#ffffff" stroke-width="1.4"/>
  </svg>`;
}

// Compact warning strip when the truth layer is not fully verified.
function renderDashTruthStrip(d) {
  const ts = d.truth_status || {};
  if (ts.overall === "ready") return "";
  const worst = [];
  if (ts.revenue !== "ready") worst.push(`Revenue: ${ts.revenue}`);
  if (ts.spend !== "ready") worst.push(`Spend: ${ts.spend}`);
  if (ts.fx !== "ready") worst.push(`FX: ${ts.fx}`);
  if (ts.source_attribution !== "ready") worst.push(`Sources: ${ts.source_attribution}`);
  const tone = ts.overall === "blocked" ? "dash-truth-strip--blocked" : "";
  const link = dashCanNavigate("revenue-health")
    ? '<a class="dash-truth-strip__link" href="#/revenue-health">Open Revenue Health</a>'
    : '<span class="dash-truth-strip__link">Contact your administrator</span>';
  return `
    <div class="dash-truth-strip ${tone}" role="status">
      <span class="dash-truth-strip__label">Partial truth</span>
      <span class="dash-truth-strip__detail">${escapeHtml(worst.join(" · "))}</span>
      ${link}
    </div>`;
}

function renderDashKpiRow(d) {
  const k = d.kpis || {};
  const pc = d.period_change || {};
  const native = k.native_spend || {};

  const spendSpark = dashSparkline(dashTrendSeries(d, "spend_usd"));
  const revenueSpark = dashSparkline(dashTrendSeries(d, "revenue_usd"));

  const nativeLine = native.amount !== null && native.amount !== undefined
    ? `${escapeHtml(fmtCompactCurrency(native.amount, native.currency || "GBP"))} native`
    : "Native spend unavailable";

  // PR-ADS-155-F2 §2: the two "% of leads" sub-lines that used to sit here are
  // gone. `sql_rate` divided campaign-attributable SQLs by mart leads — two
  // independent window totals — and `customer_rate` divided closed-won DEALS by
  // lead CONTACTS, which crossed both populations and grains and drew, in
  // words, the arrow from lifecycle progression into commercial outcome that
  // §2 forbids drawing at all. Both are permanently null on the contract now,
  // with a reason. Progression is shown as a cohort conversion in the Lifecycle
  // Activity panel, measured by the canonical funnel service over a real cohort.

  const cards = [
    {
      key: "spend",
      label: "Google Ads Spend",
      value: dashValue(k.google_ads_spend_usd, fmtMoney),
      sub: nativeLine,
      delta: dashDeltaChip("spend_usd", pc),
      spark: spendSpark,
      source: "Google Ads · USD reporting",
      ok: k.google_ads_spend_usd !== null && k.google_ads_spend_usd !== undefined,
    },
    {
      key: "revenue",
      label: "Closed-Won Revenue",
      value: canonicalValueHtml(k.closed_won_revenue_usd, (k.metrics || {}).revenue_usd, fmtMoney, { windowType: "business", windowKey: (d.window || {}).key }),
      sub: "USD · HubSpot truth",
      delta: dashDeltaChip("revenue_usd", pc),
      spark: revenueSpark,
      source: "HubSpot Closed-Won",
      ok: k.closed_won_revenue_usd !== null && k.closed_won_revenue_usd !== undefined,
    },
    {
      key: "customers",
      label: "Customers",
      // PR-ADS-161E: distinct proven HubSpot companies — the canonical customer
      // metric, never the deal count. Withheld or unavailable shows the word
      // and the reason (and, for admins, a link to the exact CRM blockers).
      value: canonicalMetricHtml(
        { value: k.customers, status: k.customers_status, reason: k.customers_reason },
        fmtCount, { windowType: "business", windowKey: (d.window || {}).key }),
      // Closed-won DEALS beside it — a separate metric. Never "n% of leads":
      // leads are contacts, and a deal is not a lifecycle stage a contact
      // converts into (PR-ADS-155-F2 §2).
      sub: closedWonDealsSubText(k.closed_won_deals, (k.metrics || {}).closed_won_deals),
      delta: dashDeltaChip("customers", pc),
      spark: "",
      source: "HubSpot companies behind closed-won deals",
      ok: k.customers_status === "published",
    },
    {
      // PR-ADS-177A — ONE SQL count: the window's Google Ads ACQUISITION
      // COHORT, the same count Dashboard Campaigns / Countries, the Countries
      // page and Campaign Evidence publish. Gated on THIS response's cohort:
      // withheld reads the word and the reason, never a number. (It replaced
      // the PR-ADS-152 legacy lead-status headline.)
      key: "google_ads_cohort_sqls",
      label: COHORT_SQLS_LABEL,
      value: cohortCountHtml(k.google_ads_cohort_sqls, d.cohort, null),
      sub: ((!cohortCountPublication(d.cohort).publish
            || k.campaign_row_cohort_sqls === null || k.campaign_row_cohort_sqls === undefined)
        ? "Google Ads contacts created in this window"
        : `${cohortCountHtml(k.campaign_row_cohort_sqls, d.cohort, null)} on a campaign row`)
        + escapeHtml(cohortThroughSuffix(d.cohort)),
      // Both periods are the cohort (the mart's summary.sqls), like for like.
      delta: cohortDeltaChip("sqls", pc, d),
      spark: "",
      source: "HubSpot acquisition cohort",
      ok: cohortCountPublication(d.cohort).publish
        && k.google_ads_cohort_sqls !== null && k.google_ads_cohort_sqls !== undefined,
    },
    {
      key: "period_revenue_per_ad_spend",
      label: PERIOD_RATIO_LABEL,
      value: dashValue(k.google_ads_period_revenue_per_ad_spend, fmtPeriodRatio),
      sub: (k.google_ads_period_revenue_per_ad_spend !== null
        && k.google_ads_period_revenue_per_ad_spend !== undefined
        ? "Verified spend + revenue · " : "Needs verified spend, FX, revenue · ")
        + PERIOD_RATIO_DISCLOSURE,
      delta: dashDeltaChip("period_revenue_per_ad_spend", pc),
      spark: "",
      source: "Google Ads only",
      ok: k.google_ads_period_revenue_per_ad_spend !== null
        && k.google_ads_period_revenue_per_ad_spend !== undefined,
    },
  ];

  const compareNote = pc.available
    ? `<p class="dash-compare-note">Change ${escapeHtml(pc.label || "vs previous period")}${pc.note ? ` — ${escapeHtml(pc.note)}` : ""}</p>`
    : `<p class="dash-compare-note dash-compare-note--muted">No previous-period comparison${pc.reason ? ` — ${escapeHtml(pc.reason)}` : ""}</p>`;

  return `
    <div class="dash-kpi-grid">
      ${cards.map((c) => `
        <div class="dash-kpi-card dash-anim ${c.ok ? "" : "dash-kpi-card--unavailable"}" data-kpi="${c.key}">
          <div class="dash-kpi-card__label">${escapeHtml(c.label)}</div>
          <div class="dash-kpi-card__value">${c.value}</div>
          <div class="dash-kpi-card__sub">${escapeHtml(c.sub)}</div>
          <div class="dash-kpi-card__meta">${c.delta}${c.spark}</div>
          <div class="dash-kpi-card__source">${escapeHtml(c.source)}</div>
        </div>`).join("")}
    </div>
    ${compareNote}`;
}

// Round a chart max up to a clean tick value (1/2/2.5/5 × 10^k).
function dashNiceCeil(v) {
  if (v <= 0) return 1;
  const exp = Math.floor(Math.log10(v));
  const base = Math.pow(10, exp);
  const unit = v / base;
  let nice;
  if (unit <= 1) nice = 1;
  else if (unit <= 2) nice = 2;
  else if (unit <= 2.5) nice = 2.5;
  else if (unit <= 5) nice = 5;
  else nice = 10;
  return nice * base;
}

function dashCompactMoney(v) {
  if (v === null || v === undefined) return "—";
  if (Math.abs(v) >= 1000000) return "$" + (v / 1000000).toFixed(1) + "M";
  if (Math.abs(v) >= 1000) return "$" + Math.round(v / 1000) + "k";
  return "$" + Math.round(v);
}

function dashBucketLabel(iso, bucket) {
  if (!iso) return "";
  const dt = new Date(`${iso}T00:00:00Z`);
  if (Number.isNaN(dt.getTime())) return iso;
  const opts = bucket === "month"
    ? { month: "short", year: "2-digit", timeZone: "UTC" }
    : { day: "2-digit", month: "short", timeZone: "UTC" };
  return dt.toLocaleDateString("en-GB", opts);
}

function renderDashTrendPanel(d) {
  const trend = d.trend || {};
  const k = d.kpis || {};
  const bucketLabel = trend.bucket === "month" ? "Monthly" : (trend.bucket === "week" ? "Weekly" : "");

  const notes = [];
  if (trend.spend_status !== "ready") {
    notes.push(`Spend series unavailable — ${trend.spend_reason || "not verified"}`);
  }
  if (trend.revenue_status !== "ready") {
    notes.push(`Revenue series unavailable — ${trend.revenue_reason || "ledger unavailable"}`);
  }

  return `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Revenue vs Spend</h3>
        <p class="dash-panel__sub">Is revenue moving with spend?${bucketLabel ? ` · ${bucketLabel}` : ""}</p>
      </div>
      <div class="dash-legend">
        <span class="dash-legend__item"><span class="dash-legend__swatch" style="background:${DASH_SERIES_COLORS.revenue}"></span>Revenue</span>
        <span class="dash-legend__item"><span class="dash-legend__line" style="background:${DASH_SERIES_COLORS.spend}"></span>Spend</span>
      </div>
    </div>
    ${notes.map((n) => `<p class="dash-series-note">${escapeHtml(n)}</p>`).join("")}
    ${dashComboChartSVG(trend)}
    <p class="dash-chart-caption">
      Window totals — Revenue: ${canonicalValueHtml(k.closed_won_revenue_usd, (k.metrics || {}).revenue_usd, fmtMoney)} ·
      Spend: ${dashValue(k.google_ads_spend_usd, fmtMoney)}
    </p>`;
}

function dashComboChartSVG(trend) {
  const points = (trend && trend.points) || [];
  const revenueReady = trend && trend.revenue_status === "ready";
  const spendReady = trend && trend.spend_status === "ready";
  if (!points.length || (!revenueReady && !spendReady)) {
    return '<div class="dash-chart-empty">No chartable trend in this window.</div>';
  }

  const W = 760, H = 240;
  const padL = 52, padR = 14, padT = 12, padB = 30;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const n = points.length;
  const slot = plotW / n;
  const barW = Math.min(24, slot * 0.55);

  const values = [];
  points.forEach((p) => {
    if (revenueReady && p.revenue_usd !== null && p.revenue_usd !== undefined) values.push(p.revenue_usd);
    if (spendReady && p.spend_usd !== null && p.spend_usd !== undefined) values.push(p.spend_usd);
  });
  const yMax = dashNiceCeil(Math.max(1, ...values));
  const xCenter = (i) => padL + slot * i + slot / 2;
  const yPos = (v) => padT + plotH * (1 - v / yMax);
  const baseline = padT + plotH;

  // Hairline solid gridlines + clean y ticks.
  const ticks = [0.25, 0.5, 0.75, 1];
  const grid = ticks.map((t) => {
    const y = yPos(yMax * t).toFixed(1);
    return `<line x1="${padL}" x2="${W - padR}" y1="${y}" y2="${y}" stroke="#e0e6ef" stroke-width="1"/>
      <text x="${padL - 8}" y="${Number(y) + 3}" text-anchor="end" class="dash-chart-tick">${dashCompactMoney(yMax * t)}</text>`;
  }).join("");

  // Revenue bars: rounded data-end, square baseline, from a single baseline.
  let bars = "";
  if (revenueReady) {
    bars = points.map((p, i) => {
      const v = p.revenue_usd;
      if (v === null || v === undefined) return "";
      const h = Math.max(0, baseline - yPos(v));
      if (h === 0) return "";
      const x = xCenter(i) - barW / 2;
      const y = yPos(v);
      const r = Math.min(4, barW / 2, h);
      return `<path d="M${x.toFixed(1)},${baseline.toFixed(1)}
        L${x.toFixed(1)},${(y + r).toFixed(1)}
        Q${x.toFixed(1)},${y.toFixed(1)} ${(x + r).toFixed(1)},${y.toFixed(1)}
        L${(x + barW - r).toFixed(1)},${y.toFixed(1)}
        Q${(x + barW).toFixed(1)},${y.toFixed(1)} ${(x + barW).toFixed(1)},${(y + r).toFixed(1)}
        L${(x + barW).toFixed(1)},${baseline.toFixed(1)} Z" fill="${DASH_SERIES_COLORS.revenue}" fill-opacity="0.92"/>`;
    }).join("");
  }

  // Spend line + soft area wash + ringed markers.
  let spendLayer = "";
  if (spendReady) {
    const linePts = points.map((p, i) =>
      `${xCenter(i).toFixed(1)},${yPos(p.spend_usd || 0).toFixed(1)}`);
    const areaPath = `M${padL + slot / 2},${baseline} L${linePts.join(" L")} L${(padL + slot * (n - 1) + slot / 2).toFixed(1)},${baseline} Z`;
    const dots = points.map((p, i) =>
      `<circle cx="${xCenter(i).toFixed(1)}" cy="${yPos(p.spend_usd || 0).toFixed(1)}" r="4"
        fill="${DASH_SERIES_COLORS.spend}" stroke="#ffffff" stroke-width="2"/>`).join("");
    spendLayer = `
      <path d="${areaPath}" fill="${DASH_SERIES_COLORS.spend}" fill-opacity="0.08"/>
      <polyline points="${linePts.join(" ")}" fill="none" stroke="${DASH_SERIES_COLORS.spend}"
        stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      ${n <= 32 ? dots : ""}`;
  }

  // Sparse x labels: first, last, and evenly-spaced in between — a modulo
  // label too close to the final label is dropped so they never collide.
  const labelEvery = Math.max(1, Math.ceil(n / 6));
  const xLabels = points.map((p, i) => {
    if (i !== 0 && i !== n - 1) {
      if (i % labelEvery !== 0) return "";
      if (n - 1 - i < Math.max(2, Math.ceil(labelEvery * 0.6))) return "";
    }
    return `<text x="${xCenter(i).toFixed(1)}" y="${H - 8}" text-anchor="middle" class="dash-chart-tick">${escapeHtml(dashBucketLabel(p.period_start, trend.bucket))}</text>`;
  }).join("");

  // Full-height hover bands: the hit target is the whole bucket slot.
  const hitBands = points.map((p, i) =>
    `<rect class="dash-chart-hit" data-dash-idx="${i}" x="${(padL + slot * i).toFixed(1)}" y="${padT}"
      width="${slot.toFixed(1)}" height="${plotH}" fill="transparent"/>`).join("");

  return `
    <div class="dash-chart-wrap">
      <svg viewBox="0 0 ${W} ${H}" class="dash-combo-chart" role="img"
        aria-label="Revenue bars and spend line per ${trend.bucket || "period"}">
        ${grid}
        <line x1="${padL}" x2="${W - padR}" y1="${baseline}" y2="${baseline}" stroke="#cbd5e1" stroke-width="1"/>
        ${bars}
        ${spendLayer}
        ${xLabels}
        ${hitBands}
      </svg>
      <div class="dash-chart-tooltip" hidden></div>
    </div>`;
}

// Crosshair-style hover: the hovered band highlights and one tooltip lists
// EVERY series at that bucket. Tooltips enhance, never gate — the caption and
// KPI cards carry the window totals as text.
function wireDashChartHover(root, d) {
  const wrap = root.querySelector(".dash-chart-wrap");
  if (!wrap) return;
  const tooltip = wrap.querySelector(".dash-chart-tooltip");
  const points = ((d.trend || {}).points) || [];

  function hide() {
    if (tooltip) tooltip.hidden = true;
    wrap.querySelectorAll(".dash-chart-hit.is-hover").forEach((el) => el.classList.remove("is-hover"));
  }

  wrap.addEventListener("pointermove", (e) => {
    const band = e.target.closest(".dash-chart-hit");
    if (!band || !tooltip) { hide(); return; }
    const idx = Number(band.dataset.dashIdx);
    const p = points[idx];
    if (!p) { hide(); return; }

    wrap.querySelectorAll(".dash-chart-hit.is-hover").forEach((el) => el.classList.remove("is-hover"));
    band.classList.add("is-hover");

    // Untrusted-data rule: tooltip content built with textContent, not HTML.
    tooltip.textContent = "";
    const title = document.createElement("div");
    title.className = "dash-chart-tooltip__title";
    title.textContent = dashBucketLabel(p.period_start, (d.trend || {}).bucket);
    tooltip.appendChild(title);
    const rows = [
      ["Revenue", p.revenue_usd, DASH_SERIES_COLORS.revenue],
      ["Spend", p.spend_usd, DASH_SERIES_COLORS.spend],
      ["Won deals", p.closed_won_deals, null],
    ];
    rows.forEach(([label, value, color]) => {
      const row = document.createElement("div");
      row.className = "dash-chart-tooltip__row";
      if (color) {
        const key = document.createElement("span");
        key.className = "dash-chart-tooltip__key";
        key.style.background = color;
        row.appendChild(key);
      }
      const val = document.createElement("strong");
      val.textContent = value === null || value === undefined
        ? "Unavailable"
        : (label === "Customers" ? String(value) : fmtMoney(value));
      const name = document.createElement("span");
      name.textContent = ` ${label}`;
      row.appendChild(val);
      row.appendChild(name);
      tooltip.appendChild(row);
    });

    const wrapRect = wrap.getBoundingClientRect();
    const bandRect = band.getBoundingClientRect();
    tooltip.hidden = false;
    const tipW = tooltip.offsetWidth || 140;
    let left = bandRect.left - wrapRect.left + bandRect.width / 2 - tipW / 2;
    left = Math.max(4, Math.min(left, wrapRect.width - tipW - 4));
    tooltip.style.left = `${left}px`;
    tooltip.style.top = "8px";
  });
  wrap.addEventListener("pointerleave", hide);
}

// ══ PR-ADS-180A — the ONE lifecycle funnel block (docs/63) ═══════════════════
//
// Every page that shows the lifecycle funnel draws it here and nowhere else:
// Overview (Lifecycle Activity, Acquisition Cohort), Pipeline, Campaigns,
// Countries and Channels. The
// block is the Dashboard's design (the owner's standard): five stage columns in
// one row, a connector between each pair, the same classes on every page.
//
// The block DRAWS; it decides nothing. Each page has an adapter that reads its
// OWN response and builds a model, and the block takes that model as its only
// data argument. It reads no page state — no cohort, verdict or selection from
// another response — and it computes no rate: every percentage it shows is one
// the adapter read from the payload. A shared design must never erase a
// per-page distinction, so the model carries the payload's own metric family
// and window basis and the block prints them on its face.
//
// The model:
//   family       {key, basis}: the payload's metric_family (or basis) and its
//                window basis. Labelled from FUNNEL_FAMILY_LABELS; a key the
//                block does not know is "not stated", never guessed.
//   verdict      {status, reason, word, note}: the whole funnel's publication.
//                Anything but "published" puts the word in every stage and no
//                figure anywhere (withheld means absent).
//   stages       up to five, each {slot, key, label, metric, bound_reason,
//                not_on_page, sub, sub_title, notes, help, action_title}.
//                `metric` is a reader-contract {value, status, reason} rendered
//                by canonicalMetricHtml: a published 0 is "0", anything else the
//                word and its reason. `bound_reason` (the reason, in words) makes
//                a published count "at least N". A slot the page does not
//                carry is "Not available on this page" — never 0, never a dash.
//   conversions  four, between adjacent slots: {kind, pct, basis, title}.
//                exact "X%" · at_least "at least X%" (PR-ADS-178B-F2: the TO
//                stage is a lower bound) · unknown "—" with the reason (the FROM
//                stage is a lower bound or unknown) · unavailable the word and
//                its reason · none: a plain connector, no rate published.
// opts           {ariaLabel, selectable, activeKey, layout}: presentation only. A
//                selectable block makes each stage a button carrying
//                data-funnel-event; the selected stage is an ARGUMENT.
//                (PR-ADS-182A: Pipeline, its one user, now draws the cohort
//                block unselectable — its contact list is a different count.)
//                PR-ADS-181A: layout "vertical" stacks the same five cells in
//                one column (the Channels drawer). Same cells, same rules: it
//                changes the CSS layout only, never what a cell says.
const FUNNEL_BLOCK_SLOTS = ["contacts", "mql", "sql", "opportunity", "customer"];
// A column's caption when the adapter gives no label of its own (a withheld or
// unavailable funnel): the owner's five columns. The last is the lifecycle
// Customer STAGE — contacts — never customers won (distinct companies).
const FUNNEL_BLOCK_SLOT_LABELS = {
  contacts: "Contacts", mql: "MQL", sql: "SQL", opportunity: "Opportunity", customer: "Customer stage",
};
const FUNNEL_BLOCK_NOT_ON_PAGE = "Not available on this page";
// Ordinal ramp for the five columns (validated with --ordinal; the Dashboard's
// funnel ramp, now the block's): each column has its own rung on every page.
const FUNNEL_BLOCK_RAMP = ["#6db8f8", "#3f9df0", "#1a80cf", "#0b5f92", "#08425f"];
// The canonical lifecycle events, by the column each one occupies. The first
// column is the population the funnel starts from: Lead entries on the
// stage-entry funnels, contacts acquired on the acquisition cohort.
const FUNNEL_EVENT_SLOTS = {
  lead: "contacts", mql: "mql", sql: "sql", opportunity: "opportunity", customer: "customer",
};
/**
 * The window basis of a stage-entry funnel, from the payload's own per-stage
 * metadata: "stage_entry_date" only when EVERY stage names its own entry date
 * (`window_basis` date_entered_<stage>, or the HubSpot date_entered_ property
 * it is read from). Anything less is not stated — never assumed.
 */
function stageEntryWindowBasis(blocks) {
  const list = Array.isArray(blocks) ? blocks : [];
  const dated = list.length > 0 && list.every((b) => !!b && (
    (typeof b.window_basis === "string" && b.window_basis.indexOf("date_entered_") === 0)
    || (typeof b.event_date_property === "string"
      && b.event_date_property.indexOf("date_entered_") !== -1)));
  return dated ? "stage_entry_date" : null;
}

/** Label-only column models for an unpublished lifecycle block: the payload's
 * own stage labels where it sent them, the canonical names otherwise. */
function lifecycleColumnLabels(stages) {
  const byEvent = {};
  (Array.isArray(stages) ? stages : []).forEach((st) => {
    if (st && st.event && st.label) byEvent[st.event] = st.label;
  });
  return Object.keys(FUNNEL_EVENT_SLOTS).map((event) => ({
    slot: FUNNEL_EVENT_SLOTS[event], key: event,
    label: byEvent[event] || LIFECYCLE_COLUMN_LABELS[event] }));
}

// The lifecycle events' own column names (analysis/crm_lifecycle EVENT_LABELS),
// for an Overview block whose payload carried none (an unavailable verdict).
// PR-ADS-180A-F1: the first column there is Lead entries, never "Contacts".
const LIFECYCLE_COLUMN_LABELS = {
  lead: "Leads", mql: "MQLs", sql: "SQLs", opportunity: "Opportunities",
  customer: "Moved to Customer stage",
};

/** A raw payload count as a reader-contract metric: null is unknown, never 0. */
function funnelCountMetric(value, reason) {
  const known = value !== null && value !== undefined;
  return { value: known ? value : null, status: known ? "published" : "unavailable",
    reason: known ? null : (reason || null) };
}

function funnelBlockHtml(model, opts) {
  const m = model || {};
  const o = opts || {};
  // Fail closed: a model that states no verdict is not evidence of one.
  const verdict = m.verdict && m.verdict.status ? m.verdict
    : { status: "unavailable", reason: "this response states no publication verdict for the funnel" };
  const given = Array.isArray(m.stages) ? m.stages : [];
  const conversions = Array.isArray(m.conversions) ? m.conversions : [];
  const cells = FUNNEL_BLOCK_SLOTS.map((slot, i) => {
    const stage = given.find((s) => s && s.slot === slot)
      || { slot, key: slot, not_on_page: "this page's response carries no figure for this stage" };
    const link = i === 0 ? "" : funnelBlockConvHtml(conversions[i - 1], verdict);
    return `${link}${funnelBlockStageHtml(stage, i, verdict, o)}`;
  }).join("");
  const family = funnelBlockFamily(m.family);
  const layout = o.layout === "vertical" ? ' data-funnel-layout="vertical"' : "";
  return `
    <div class="funnel-block"${layout} data-metric-family="${escapeHtml(family.key)}" data-window-basis="${escapeHtml(family.basis)}">
      <p class="funnel-block__family"><span class="funnel-block__family-label">${escapeHtml(family.label)}</span> · <span class="funnel-block__basis">${escapeHtml(family.basisLabel)}</span></p>
      ${funnelBlockVerdictHtml(verdict)}
      <div class="dash-funnel" role="group" aria-label="${escapeHtml(o.ariaLabel || family.label)}">${cells}</div>
    </div>`;
}

// The family and window basis exactly as the payload stated them. Missing or
// unknown is said so — the block never supplies a family the payload did not.
function funnelBlockFamily(family) {
  const f = family || {};
  const key = f.key ? String(f.key) : "";
  const basis = f.basis ? String(f.basis) : "";
  return {
    key: key || "not_stated", basis: basis || "not_stated",
    label: FUNNEL_FAMILY_LABELS[key] || "Metric family not stated by this response",
    basisLabel: FUNNEL_WINDOW_BASIS_LABELS[basis] || "window basis not stated by this response",
  };
}
// What each published metric family IS. The key comes from the payload; the
// block only names it. Two pages may show two different families — the block
// says which, and never relabels one as the other.
const FUNNEL_FAMILY_LABELS = {
  lifecycle_stage_events: "Lifecycle stage events: contacts that entered each stage in the window",
  stage_entry_events: "Lifecycle stage events: contacts that entered each stage in the window",
  acquisition_cohort_outcomes: "Acquisition cohort: contacts created in the window; each stage counts every contact that has reached it so far",
};
const FUNNEL_WINDOW_BASIS_LABELS = {
  stage_entry_date: "each stage counted on its own HubSpot stage-entry date",
  contact_created_at: "windowed on the contact's creation date",
};


function funnelBlockVerdictHtml(verdict) {
  if (verdict.status === "published") return "";
  const word = verdict.word || (verdict.status === "withheld" ? "Withheld" : "Unavailable");
  const reason = verdict.reason ? ` — ${escapeHtml(verdict.reason)}.` : ".";
  const note = verdict.note ? ` ${escapeHtml(verdict.note)}` : "";
  return `<p class="funnel-block__verdict" data-funnel-verdict="${escapeHtml(verdict.status)}"><strong>${escapeHtml(word)}</strong>${reason}${note}</p>`;
}

function funnelBlockStageHtml(stage, index, verdict, o) {
  const s = stage || {};
  const notes = (verdict.status === "published" && Array.isArray(s.notes) ? s.notes : [])
    .filter((n) => n && n.text)
    .map((n) => `<span class="dash-funnel__note${n.tone ? ` dash-funnel__note--${escapeHtml(n.tone)}` : ""}"${
      n.title ? ` title="${escapeHtml(n.title)}"` : ""}>${escapeHtml(n.text)}</span>`).join("");
  const m = s.metric || {};
  const bounded = verdict.status === "published" && !s.not_on_page && !!s.bound_reason
    && m.status === "published" && m.value !== null && m.value !== undefined;
  const boundNote = bounded
    ? `<span class="dash-funnel__note dash-funnel__note--bound">Lower bound: ${escapeHtml(s.bound_reason)}</span>` : "";
  const help = s.help ? `<span class="dash-funnel__help">${escapeHtml(s.help)}</span>` : "";
  const sub = (verdict.status === "published" && s.sub) ? escapeHtml(s.sub) : "";
  const body = `
        <span class="dash-funnel__stage-label">${escapeHtml(s.label || FUNNEL_BLOCK_SLOT_LABELS[s.slot] || "")}</span>
        <span class="dash-funnel__stage-value">${funnelBlockValueHtml(s, verdict)}</span>
        <span class="dash-funnel__stage-sub"${s.sub_title ? ` title="${escapeHtml(s.sub_title)}"` : ""}>${sub}</span>
        ${boundNote}${notes}${help}`;
  const accent = `--funnel-accent:${FUNNEL_BLOCK_RAMP[index % FUNNEL_BLOCK_RAMP.length]}`;
  if (o.selectable) {
    const active = o.activeKey !== null && o.activeKey !== undefined && o.activeKey === s.key;
    return `
      <button type="button" class="dash-funnel__stage dash-funnel__stage--selectable${active ? " is-active" : ""}" style="${accent}"
        data-funnel-slot="${escapeHtml(s.slot)}" data-funnel-event="${escapeHtml(s.key)}" aria-pressed="${active ? "true" : "false"}"${
        s.action_title ? ` title="${escapeHtml(s.action_title)}"` : ""}>${body}
      </button>`;
  }
  return `
      <div class="dash-funnel__stage" style="${accent}" data-funnel-slot="${escapeHtml(s.slot)}">${body}
      </div>`;
}

// One stage value. Withheld means absent: under an unpublished verdict no
// figure is read at all. A published count goes through the reader-contract
// renderer; a lower bound is "at least N" with its reason.
function funnelBlockValueHtml(stage, verdict) {
  if (verdict.status !== "published") {
    if (verdict.word) return `<span class="dash-unavailable">${escapeHtml(verdict.word)}</span>`;
    return canonicalMetricHtml({ value: null, status: verdict.status, reason: null }, fmtCount, null);
  }
  if (stage.not_on_page) {
    return `<span class="dash-unavailable funnel-block__not-on-page" title="${escapeHtml(stage.not_on_page)}">${FUNNEL_BLOCK_NOT_ON_PAGE}</span>`
      + ` <small class="metric-reason">${escapeHtml(stage.not_on_page)}</small>`;
  }
  const m = stage.metric || {};
  const published = m.status === "published" && m.value !== null && m.value !== undefined;
  if (published && stage.bound_reason) {
    return `<span class="dash-lower-bound" data-lower-bound="${escapeHtml(stage.key)}" title="${escapeHtml(stage.bound_reason)}">at least ${escapeHtml(fmtCount(m.value))}</span>`;
  }
  return canonicalMetricHtml(m, fmtCount, null);
}

// One connector. The rate is the adapter's, read whole from the payload.
function funnelBlockConvHtml(conv, verdict) {
  const c = (verdict.status === "published" && conv) ? conv : { kind: "none" };
  const pct = c.pct === null || c.pct === undefined ? null : escapeHtml(String(c.pct));
  const title = c.title ? ` title="${escapeHtml(c.title)}"` : "";
  let chip = "";
  if (c.kind === "exact" && pct !== null) {
    chip = `<span class="dash-funnel__conv"${title}>${pct}%</span>`;
  } else if (c.kind === "at_least" && pct !== null) {
    chip = `<span class="dash-funnel__conv" data-conv-bound="at_least"${title}>at least ${pct}%</span>`;
  } else if (c.kind === "unknown") {
    chip = `<span class="dash-funnel__conv dash-funnel__conv--muted" data-conv-bound="unknown"${title}>—</span>`;
  } else if (c.kind === "unavailable" || c.kind === "exact" || c.kind === "at_least") {
    // An exact or at-least chip without its rate is unavailable, never 0%.
    chip = `<span class="dash-funnel__conv dash-funnel__conv--muted" data-conv-unavailable${title}>Unavailable</span>`;
  }
  const basis = chip && c.basis ? `<span class="dash-funnel__basis">${escapeHtml(c.basis)}</span>` : "";
  return `
      <div class="dash-funnel__link${chip ? "" : " dash-funnel__link--plain"}"${chip ? "" : ' aria-hidden="true"'}><span class="dash-funnel__arrow" aria-hidden="true">→</span>${chip}${basis}</div>`;
}

/**
 * Which stage-entry counts are lower bounds, each with its reason in words
 * (PR-ADS-178B-F1 o1; Overview's Lifecycle Activity — the Pipeline funnel
 * left the stage-entry family in PR-ADS-182A).
 *
 * Under `reconciliation.status === "partial"` a stage some contacts reached
 * with no recorded entry date counts only the dated entries: the undated ones
 * may belong to this window. The payload's coverage names the stages; when it
 * does not (no per-stage coverage), EVERY stage is a lower bound — never
 * assume exactness the evidence does not carry.
 */
function stageEntryLowerBounds(f, events) {
  const out = {};
  const recon = (f && f.reconciliation) || {};
  if (recon.status !== "partial") return out;
  const missing = (((f && f.coverage) || {}).stage_reached_without_entry_date);
  const perStage = missing && typeof missing === "object";
  events.forEach((event) => {
    if (!perStage) {
      out[event] = canonicalReasonLabel("missing_stage_entry_date")
        + " (the evidence does not say which stages)";
      return;
    }
    const n = missing[event];
    if (n === null || n === undefined) {
      out[event] = canonicalReasonLabel("missing_stage_entry_date")
        + " (not reported for this stage)";
    } else if (Number(n) > 0) {
      out[event] = `${fmtCount(n)} contact${Number(n) === 1 ? "" : "s"} reached this stage with no entry date recorded in HubSpot`;
    }
  });
  return out;
}

/**
 * One cohort-safe conversion as a connector model, under 178B-F2's rule. An
 * undated FROM stage leaves the cohort itself incomplete: the rate is unknown
 * ("—" with the reason). An undated TO stage only undercounts the converted
 * contacts: the rate is "at least X%". `conv` is the canonical funnel's own
 * conversion object; nothing here divides.
 */
function stageEntryConversion(conv, fromBound, toBound, exactTitle, basis) {
  const ok = !!conv && conv.available === true && conv.basis === "cohort"
    && conv.rate_pct !== null && conv.rate_pct !== undefined;
  if (!ok) return null;
  if (fromBound) return { kind: "unknown", title: `Conversion unknown: ${fromBound}` };
  if (toBound) {
    return { kind: "at_least", pct: conv.rate_pct,
      title: exactTitle ? `Lower bound: ${toBound}. ${exactTitle}` : `Lower bound: ${toBound}`,
      basis: basis ? `at least ${basis}` : "" };
  }
  return { kind: "exact", pct: conv.rate_pct, title: exactTitle, basis: basis || "" };
}
// ══ end of the funnel block ══════════════════════════════════════════════════

/**
 * PR-ADS-155-F2 §1/§2 — lifecycle ACTIVITY in the selected window.
 *
 * What this is, and what it deliberately is not
 * --------------------------------------------
 * Five stage-entry counts: how many contacts entered Lead, MQL, SQL,
 * Opportunity and Customer DURING this window. They are five independent
 * populations, so they do not descend and are not made to. On production this
 * quarter they run 352 → 553 → 42 → 46 → 17, MQL above Lead, because a contact
 * can enter MQL now having entered Lead two years ago. That is correct data.
 *
 * The percentages between the cards are therefore NOT the ratio of one card to
 * the next — that quotient compares two different populations and means
 * nothing. Each one is the canonical funnel service's cohort conversion, read
 * whole from `lifecycle_activity.conversions`, and each is rendered beside the
 * cohort it was measured over so the denominator is never left to the reader's
 * imagination. This function performs no arithmetic on any count.
 *
 * Previous-period movement is a third, separate thing. It sits INSIDE a stage
 * card, labelled as a comparison, never between two cards where a reader would
 * take it for progression.
 *
 * PR-ADS-180A: drawn by the shared funnel block (funnelBlockHtml) from
 * dashActivityFunnelModel. A count some contacts reached with no entry date is
 * a lower bound (stageEntryCountBounds).
 */
function renderDashLifecycleActivity(d) {
  const activity = d.lifecycle_activity || null;
  const available = !!activity && activity.available === true;
  return `
    <section class="dash-panel dash-activity-panel dash-anim" aria-label="Lifecycle activity">
      <div class="dash-panel__header">
        <div>
          <h3 class="dash-panel__title">Lifecycle Activity</h3>
          <p class="dash-panel__sub">${escapeHtml(DASH_ACTIVITY_SUBTITLE)}</p>
        </div>
        ${available ? dashActivityCoverageBadge(activity) : ""}
      </div>
      ${funnelBlockHtml(dashActivityFunnelModel(activity), { ariaLabel: "Lifecycle activity" })}
      ${available ? `<p class="lifecycle-legend">Percentages between stages are cohort
        conversions from the canonical CRM funnel, each measured over the cohort
        named beside it — never one card divided by another.</p>
      ${dashActivityCoverageDisclosure(activity)}` : ""}
    </section>`;
}

/**
 * PR-ADS-180A — the activity block's model, read from `lifecycle_activity`
 * alone. The family is the block's own `basis` ("stage_entry_events"); a count
 * some contacts reached with no entry date is a lower bound
 * (stageEntryCountBounds); every connector is the canonical
 * cohort conversion, whole.
 */
function dashActivityFunnelModel(activity) {
  const a = activity || {};
  const stages = Array.isArray(a.stages) ? a.stages : [];
  const family = { key: a.basis, basis: stageEntryWindowBasis(stages) };
  if (a.available !== true) {
    return { family, verdict: { status: "unavailable", reason: dashActivityReasonLabel(a.reason) },
      stages: lifecycleColumnLabels(stages) };
  }
  const bounds = stageEntryCountBounds(a, stages);
  const conversions = Array.isArray(a.conversions) ? a.conversions : [];
  const byPair = {};
  conversions.forEach((c) => { byPair[`${c.from_event}>${c.to_event}`] = c; });
  return {
    family,
    verdict: { status: "published" },
    stages: stages.map((s) => ({
      slot: FUNNEL_EVENT_SLOTS[s.event], key: s.event, label: s.label || s.event || "",
      metric: funnelCountMetric(s.entered, null),
      bound_reason: bounds[s.event] || null,
      sub: s.activity_label || "Entered during this period",
      sub_title: s.event_date_property ? `HubSpot property: ${s.event_date_property}` : "",
      // §5: the trend lives INSIDE the stage, never between two stages.
      // PR-ADS-180A-F1: an exact change beside "at least N" would claim a
      // measurement the count is not; a bound stage gets no comparison.
      notes: [bounds[s.event]
        ? { text: "No comparison", tone: "none",
            title: `This count is a lower bound (${bounds[s.event]}), so its change against the previous period is not measured.` }
        : dashActivityTrend(s.previous_period, a.previous_period)],
    })),
    conversions: stages.slice(1).map((s, i) => dashActivityConversion(
      byPair[`${stages[i].event}>${s.event}`], bounds[stages[i].event], bounds[s.event])),
  };
}

/**
 * The lower bounds of a stage-entry funnel, each with its reason in words —
 * Overview's Lifecycle Activity (PR-ADS-180B / 180C, docs/65). PR-ADS-182A:
 * the Pipeline funnel is the acquisition cohort now (acquisitionFunnelModel),
 * so Activity is the one stage-entry funnel left, and the lead-filter branch
 * (`undated_contacts_all_sources`, Pipeline's) went with it.
 *
 * `block` is the funnel block (`lifecycle_activity`); `stages` are its
 * stages, each `{event, undated_contacts_could_belong, count_is_bound}`.
 *
 * The window-row rule (stageEntryLowerBounds) reads the gap in the window's
 * OWN rows; a contact that reached a stage undated but has no date in the
 * window is not among them, so "0 there" never proved a count exact. Each
 * stage also reads `undated_contacts_could_belong`: the same gap over EVERY
 * contact that could belong to the window. Above zero, or not measured, the
 * count is a bound. A stage whose server verdict (`count_is_bound`) is not
 * an explicit `false` is a bound too: missing is not "complete".
 */
function stageEntryCountBounds(block, stages) {
  const list = Array.isArray(stages) ? stages : [];
  const out = stageEntryLowerBounds(block, list.map((s) => s.event));
  const measured = ((block || {}).undated_reach || {}).available === true;
  list.forEach((s) => {
    const n = s.undated_contacts_could_belong;
    if (!measured || n === null || n === undefined) {
      out[s.event] = "the contacts that reached this stage with no entry date could not all be counted, so this count may be missing some";
    } else if (Number(n) > 0) {
      out[s.event] = `${fmtCount(n)} contact${Number(n) === 1 ? "" : "s"} reached this stage with no entry date recorded in HubSpot and could belong to this window`;
    }
    // PR-ADS-180B-F1: the server's own verdict on the count is republished,
    // never overruled. PR-ADS-180C: fail closed — a stage the server does
    // not state complete (`count_is_bound` true, or missing) is a bound even
    // where the evidence above names no reason. The page's own rules still
    // bound a stage the server states exact; they only ever add a bound.
    if (!out[s.event] && s.count_is_bound !== false) {
      out[s.event] = "the server does not state this count complete: the evidence does not show every contact that reached this stage with an entry date";
    }
  });
  return out;
}

const DASH_ACTIVITY_SUBTITLE =
  "How many contacts entered each lifecycle stage during this window. "
  + "Five independent populations — a later stage can exceed an earlier one, "
  + "and that is not an error.";

function dashActivityReasonLabel(reason) {
  const labels = {
    canonical_contact_store_unavailable: "the canonical contact store could not be read",
    canonical_funnel_mismatch: "a funnel invariant broke, so no count is rendered",
    canonical_funnel_read_failed: "the canonical funnel could not be built",
  };
  return labels[reason] || "canonical lifecycle activity is not available";
}

/**
 * One connector's model. Fails closed: an unavailable conversion says so, and
 * is never rendered as 0%. A genuine zero — cohort proven, nobody progressed —
 * renders as 0%, because that is a measurement and withholding it would be its
 * own lie. Beside a lower-bound stage the 178B-F2 rule applies (at least X%, or
 * unknown). The rate is `conversion.rate_pct`, whole; nothing here computes.
 */
function dashActivityConversion(conversion, fromBound, toBound) {
  if (!conversion || !conversion.available
      || conversion.rate_pct === null || conversion.rate_pct === undefined
      || conversion.cohort_size === null || conversion.cohort_size === undefined
      || conversion.converted === null || conversion.converted === undefined) {
    const reason = dashActivityConversionReason((conversion || {}).reason);
    return { kind: "unavailable", title: reason, basis: reason };
  }
  const basis = `${conversion.converted} of ${conversion.cohort_size} ${conversion.cohort_label || ""}`.trim();
  const title = `Cohort conversion from the canonical CRM funnel: of the ${conversion.cohort_size} contacts `
    + `that entered ${conversion.from_label || ""} in this window, ${conversion.converted} went on to enter ${conversion.to_label || ""}.`;
  return stageEntryConversion(conversion, fromBound, toBound, title, basis)
    || { kind: "unavailable", title: dashActivityConversionReason(null), basis: dashActivityConversionReason(null) };
}

function dashActivityConversionReason(reason) {
  const labels = {
    empty_cohort: "no contacts entered the earlier stage in this window, so there is no cohort to measure",
    unsupported_transition: "this transition is not a governed lifecycle progression",
  };
  return labels[reason] || "the canonical cohort conversion is not available";
}

/**
 * §5 — a trend, labelled as a trend: a note INSIDE the stage, never a
 * connector between two stages. A missing or zero baseline is stated rather
 * than turned into a percentage: a decline against nothing is not a decline.
 */
function dashActivityTrend(metric, previousPeriod) {
  const label = (previousPeriod && previousPeriod.label) || "vs previous period";
  if (!metric || metric.status !== "ok"
      || metric.delta_pct === null || metric.delta_pct === undefined) {
    // PR-ADS-180B: the reason travels with it ("previous count is a lower bound").
    return { text: "No comparison", tone: "none", title: (metric && metric.reason) || "" };
  }
  const pct = metric.delta_pct * 100;
  const magnitude = Math.abs(pct) >= 100 ? 0 : 1;
  const text = `${pct > 0 ? "+" : ""}${pct.toFixed(magnitude)}%`;
  const tone = metric.direction === "up" ? "up" : (metric.direction === "down" ? "down" : "");
  return { text: `${text} ${label}`, tone };
}

/** §6 — partial historical coverage, stated on the face of the panel. */
function dashActivityCoverageBadge(activity) {
  const status = ((activity || {}).reconciliation || {}).status;
  if (status === "partial") {
    return '<span class="dash-badge dash-badge--warn" title="Some contacts reached a '
      + 'lifecycle stage that HubSpot holds no entry timestamp for. They are excluded '
      + 'from every window’s stage-entry total and listed below — no substitute '
      + 'date is ever used.">Partial historical coverage</span>';
  }
  if (status === "reconciled") {
    return '<span class="dash-badge dash-badge--ok">Complete stage-entry coverage</span>';
  }
  return '<span class="dash-badge dash-badge--muted">Coverage unknown</span>';
}

function dashActivityReconciliationLabel(reason) {
  const labels = {
    missing_stage_entry_date: "Contacts reached a lifecycle stage with no entry timestamp in HubSpot",
    unknown_lifecycle_stage: "Contacts hold a lifecycle stage this funnel does not recognise",
    campaign_identity_unavailable: "The Google Ads campaign-identity contract could not be consulted",
    canonical_contact_store_unavailable: "The canonical contact store could not be read",
    canonical_funnel_read_failed: "The canonical funnel could not be built",
  };
  if (labels[reason]) return labels[reason];
  if (typeof reason === "string" && reason.indexOf("scope_nesting_broken:") === 0) {
    return `A scope nesting invariant broke at ${reason.split(":")[1]}`;
  }
  return reason;
}

function dashActivityCoverageDisclosure(activity) {
  const recon = activity.reconciliation || {};
  const reasons = Array.isArray(recon.reasons) ? recon.reasons : [];
  const stages = Array.isArray(activity.stages) ? activity.stages : [];
  const gaps = stages.filter((s) => s.missing_entry_date_contacts);
  if (!reasons.length && !gaps.length) return "";
  const reasonItems = reasons.map((r) => `
    <li class="dash-coverage__item">
      <span class="dash-coverage__count">·</span>
      <span class="dash-coverage__reason">${escapeHtml(
        dashActivityReconciliationLabel(r))}</span>
      <span class="dash-coverage__detail"></span>
    </li>`).join("");
  const gapItems = gaps.map((s) => `
    <li class="dash-coverage__item">
      <span class="dash-coverage__count">${escapeHtml(
        String(s.missing_entry_date_contacts))}</span>
      <span class="dash-coverage__reason">Reached ${escapeHtml(s.label)}, no entry timestamp</span>
      <span class="dash-coverage__detail">Excluded from the ${escapeHtml(
        s.label)} stage-entry total for every window</span>
    </li>`).join("");
  return `
    <details class="dash-coverage">
      <summary class="dash-coverage__summary">Historical coverage — what these counts can and cannot include</summary>
      <ul class="dash-coverage__list">${reasonItems}${gapItems}</ul>
      <p class="dash-coverage__note">A missing stage-entry timestamp is never replaced
        with contact creation date, current-stage date, or an ingestion timestamp: the
        transition is real, the date is unknown, and it is reported as unknown. Cohort
        conversions above are measured on the evidence that does exist, and stage-entry
        totals across one window are not expected to behave like a single closed cohort.</p>
    </details>`;
}

/**
 * PR-ADS-180B — the Overview funnel: the ACQUISITION COHORT, all sources.
 *
 * Every HubSpot contact created in this window and how many have reached each
 * stage so far (`lifecycle_acquisition_funnel`, the all-sources slot of the
 * cohort the Overview's Google Ads cohort counts come from, under its one
 * verdict). It replaced the Lead-anchored "Lifecycle Cohort" (PR-ADS-155),
 * which needed a Lead-entry date that about 60% of production MQLs lack, so
 * every stage read "at least N" and every rate "—". This one needs no
 * stage-entry date. Drawn by the shared block through the acquisition adapter
 * Channels uses; nothing here divides.
 */
function renderDashAcquisitionFunnel(d) {
  const f = d.lifecycle_acquisition_funnel || null;
  const published = !!f && ((f.publication || {}).status === "published") && !!f.totals;
  const asOf = published && f.stale && f.as_of
    ? ` <small class="metric-reason cohort-as-of">measured as of ${escapeHtml(fmtDate(f.as_of))}</small>` : "";
  return `
    <section class="dash-panel dash-cohort-panel dash-anim" aria-label="Acquisition cohort funnel">
      <div class="dash-panel__header">
        <div>
          <h3 class="dash-panel__title">Acquisition Cohort</h3>
          <p class="dash-panel__sub">Every HubSpot contact created in this window, all
            sources, and how many have reached each stage so far.${asOf}</p>
        </div>
      </div>
      ${funnelBlockHtml(acquisitionFunnelModel(f, {
        head: "Contacts acquired", headSub: "created in this window · all sources",
        reasonText: cohortFunnelReasonText, shares: false }), { ariaLabel: "Acquisition cohort" })}
      <p class="lifecycle-legend">${escapeHtml(ACQUISITION_FUNNEL_LEGEND)}</p>
    </section>`;
}

/**
 * PR-ADS-155 §2/§5 — commercial outcomes, deliberately OUTSIDE the funnel.
 *
 * A lifecycle Customer is a HubSpot contact-stage fact. A closed-won customer is
 * a deal fact. No governed contact-to-deal cohort contract exists that would let
 * one be drawn as a conversion of the other, so these never appear inside the
 * lifecycle strip and are never joined to it by an arrow.
 *
 * The revenue half is fail-closed: the total is Unavailable whenever any won
 * deal has no proven amount, and the sum of the priced ones appears ONLY under
 * the label the backend supplies, which names its own denominator.
 */
function renderDashCommercialOutcomes(d) {
  const o = d.commercial_outcomes || {};
  const knownLabel = o.known_revenue_label || "Known revenue from priced deals";
  const blocked = o.total_revenue_publishable
    ? ""
    : `<p class="dash-outcomes__blocked">Total revenue is ${dashValue(null)}: ${escapeHtml(
        dashRevenueBlockLabel(o))}</p>`;
  return `
    <section class="dash-panel dash-outcomes-panel dash-anim" aria-label="Commercial outcomes">
      <div class="dash-panel__header">
        <div>
          <h3 class="dash-panel__title">Commercial Outcomes</h3>
          <p class="dash-panel__sub">Closed-won deals. A separate truth from the lifecycle
            stages above — not a stage of that funnel.</p>
        </div>
      </div>
      <div class="dash-outcomes">
        <div class="dash-outcomes__cell">
          <div class="dash-outcomes__label">Closed-Won Deals</div>
          <div class="dash-outcomes__value">${dashValue(o.closed_won_deals, fmtCount)}</div>
          <div class="dash-outcomes__sub">Complete count — independent of deal amounts</div>
        </div>
        <div class="dash-outcomes__cell">
          <div class="dash-outcomes__label">Customers</div>
          <div class="dash-outcomes__value">${canonicalMetricHtml(
            { value: o.customers, status: o.customers_status, reason: o.customers_reason },
            fmtCount, { windowType: "business", windowKey: (d.window || {}).key })}</div>
          <div class="dash-outcomes__sub">Distinct HubSpot companies behind those deals</div>
        </div>
        <div class="dash-outcomes__cell">
          <div class="dash-outcomes__label">Total Closed-Won Revenue</div>
          <div class="dash-outcomes__value">${canonicalValueHtml(o.total_revenue_usd, (o.metrics || {}).revenue_usd, fmtMoney, { windowType: "business", windowKey: (d.window || {}).key })}</div>
          <div class="dash-outcomes__sub">USD · all sources</div>
        </div>
        <div class="dash-outcomes__cell">
          <div class="dash-outcomes__label">${escapeHtml(knownLabel)}</div>
          <div class="dash-outcomes__value dash-outcomes__value--partial">${dashValue(
            o.known_revenue_usd, fmtMoney)}</div>
          <div class="dash-outcomes__sub">A named subset — not the window total</div>
        </div>
        <div class="dash-outcomes__cell">
          <div class="dash-outcomes__label">Deals Missing an Amount</div>
          <div class="dash-outcomes__value">${dashValue(o.revenue_unavailable_deals, fmtCount)}</div>
          <div class="dash-outcomes__sub">Price these in HubSpot to unblock the total</div>
        </div>
      </div>
      ${blocked}
    </section>`;
}

function dashRevenueBlockLabel(o) {
  const labels = {
    closed_won_deals_missing_amount:
      "closed-won deals in this window have no amount in HubSpot, so the total is unknown",
    canonical_closed_won_population_unavailable: "the canonical closed-won population is unavailable",
    canonical_ledger_unreadable: "the canonical deal ledger could not be read",
    canonical_coverage_not_proven: "the canonical deal ledger's coverage is not proven",
    canonical_ledger_stale: "the deal sync is overdue, so the canonical figures may be out of date",
    canonical_sync_state_unreadable: "the canonical ledger's sync state could not be read",
  };
  return labels[o.unavailable_reason] || "the canonical revenue contract withheld it";
}

function renderDashSourceMix(d) {
  const mix = d.source_mix || [];
  // Revenue can be UNKNOWN (integration not connected) — distinct from a
  // genuine $0. Unknown renders "Unavailable"; only known values sum.
  const revenueKnown = mix.some((s) => s.revenue_usd !== null && s.revenue_usd !== undefined);
  const total = mix.reduce((acc, s) => acc + (s.revenue_usd || 0), 0);

  const legend = mix.map((s) => {
    const color = DASH_DONUT_COLORS[s.key] || "#94a3b8";
    const needsReview = s.key === "unclassified" &&
      ((s.closed_won_deals || 0) > 0 || (s.revenue_usd || 0) > 0 || (s.leads || 0) > 0);
    // "Unclassified / Needs Review" carries its flag as a chip — keep the
    // label itself short so neither is lost to ellipsis truncation.
    const label = s.key === "unclassified" ? "Unclassified" : (s.label || s.key);
    // No ROAS on mix rows (not even Google Ads) — the hero KPI carries the one
    // canonical, safety-gated ROAS on this page.
    const custPart = s.closed_won_deals === null || s.closed_won_deals === undefined
      ? "" : ` <small>· ${escapeHtml(fmtCount(s.closed_won_deals))} won</small>`;
    return `
      <li class="dash-mix-row ${s.key === "offline" ? "dash-mix-row--muted" : ""}">
        <span class="dash-mix-row__swatch" style="background:${color}"></span>
        <span class="dash-mix-row__label">${escapeHtml(label)}${needsReview ? ' <span class="dash-mix-row__flag">Needs review</span>' : ""}</span>
        <span class="dash-mix-row__value">${dashValue(s.revenue_usd, fmtMoney)}${custPart}</span>
        <span class="dash-mix-row__status">${escapeHtml(s.status || "")}</span>
      </li>`;
  }).join("");

  // Source attribution and the deal ledger are two durable views of the same
  // closed-won truth; if they disagree beyond tolerance, say so — never let a
  // pretty donut silently contradict the revenue KPI.
  const kpiRevenue = (d.kpis || {}).closed_won_revenue_usd;
  let reconcileNote = "";
  if (kpiRevenue !== null && kpiRevenue !== undefined && kpiRevenue > 0 && total > 0) {
    const driftPct = Math.abs(total - kpiRevenue) / kpiRevenue;
    if (driftPct > 0.02) {
      reconcileNote = `<p class="dash-series-note">Source-attributed total (${escapeHtml(fmtMoney(total))}) differs from closed-won revenue truth (${escapeHtml(fmtMoney(kpiRevenue))}) — source attribution is incomplete. See Revenue Health.</p>`;
    }
  }

  return `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Channel Mix</h3>
        <p class="dash-panel__sub">Closed-won revenue by acquisition source</p>
      </div>
    </div>
    ${reconcileNote}
    <div class="dash-mix">
      ${dashDonutSVG(mix, total, revenueKnown)}
      <ul class="dash-mix-list">${legend}</ul>
    </div>`;
}

function dashDonutSVG(mix, total, revenueKnown) {
  const size = 168, r = 58, cx = size / 2, cy = size / 2, stroke = 20;
  const C = 2 * Math.PI * r;
  if (!total) {
    // Unknown revenue (integration not connected) is stated as Unavailable —
    // an empty ring must never imply a verified "$0 from every source".
    const centerTop = revenueKnown ? "—" : "Unavailable";
    const centerSub = revenueKnown ? "No revenue" : "Revenue blocked";
    return `
      <div class="dash-donut">
        <svg viewBox="0 0 ${size} ${size}" role="img" aria-label="${revenueKnown ? "No closed-won revenue in this window" : "Closed-won revenue unavailable"}">
          <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="#e0e6ef" stroke-width="${stroke}"/>
        </svg>
        <div class="dash-donut__center ${revenueKnown ? "" : "dash-donut__center--blocked"}"><strong>${centerTop}</strong><span>${centerSub}</span></div>
      </div>`;
  }
  const segs = mix.filter((s) => (s.revenue_usd || 0) > 0);
  const gap = segs.length > 1 ? 2 : 0; // 2px surface gap between fills
  let offset = 0;
  const circles = segs.map((s) => {
    const frac = (s.revenue_usd || 0) / total;
    const arc = Math.max(0, frac * C - gap);
    const color = DASH_DONUT_COLORS[s.key] || "#94a3b8";
    const el = `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none"
      stroke="${color}" stroke-width="${stroke}"
      stroke-dasharray="${arc.toFixed(2)} ${(C - arc).toFixed(2)}"
      stroke-dashoffset="${(-offset).toFixed(2)}"
      data-dash-mix="${escapeHtml(s.key)}">
      <title>${escapeHtml(`${s.label}: ${fmtMoney(s.revenue_usd)} (${Math.round(frac * 100)}%)`)}</title>
    </circle>`;
    offset += frac * C;
    return el;
  }).join("");
  return `
    <div class="dash-donut">
      <svg viewBox="0 0 ${size} ${size}" role="img" aria-label="Closed-won revenue share by source group" style="transform:rotate(-90deg)">
        ${circles}
      </svg>
      <div class="dash-donut__center"><strong>${escapeHtml(fmtMoney(total))}</strong><span>closed-won</span></div>
    </div>`;
}

const DASH_SIGNAL_DEFS = [
  { key: "best_campaign", label: "Best Campaign" },
  { key: "best_country", label: "Best Country" },
  { key: "best_source", label: "Best Source" },
  { key: "biggest_waste", label: "Biggest Waste Signal" },
  { key: "fastest_improving", label: "Fastest Improving" },
  { key: "biggest_decline", label: "Biggest Decline" },
];

// PR-ADS-177C-F1/F2 (truth audit M-1): a campaign signal names a distinct
// campaign when one qualifies, and says how many shared-name rows it passed
// over; when it names a shared-name row (the only candidate) it carries that
// row's label; a total that includes shared-name rows says so.
function dashSharedExcludedNote(s) {
  const n = s && s.shared_name_rows_excluded;
  return n ? ` · ${n} shared-name row${n === 1 ? "" : "s"} (several campaigns combined) not ranked` : "";
}
function dashSharedIncludedNote(s) {
  const n = s && s.shared_name_rows_included;
  return n ? ` · includes ${n} name${n === 1 ? "" : "s"} several Google Ads campaigns share (figures combined)` : "";
}
function dashSharedNamed(name, label) {
  return label ? `${name || "—"} (${label})` : (name || "—");
}

function dashSignalBody(key, s) {
  if (key === "best_campaign" || key === "best_country") {
    const ratio = s.period_revenue_per_ad_spend !== null && s.period_revenue_per_ad_spend !== undefined
      ? ` · ${fmtPeriodRatio(s.period_revenue_per_ad_spend)} ${PERIOD_RATIO_LABEL} (${PERIOD_RATIO_DISCLOSURE})` : "";
    return {
      value: s.label,
      line: `${s.shared_name_label ? `${s.shared_name_label} · ` : ""}${fmtMoney(s.revenue_usd)} closed-won · ${fmtCount(s.closed_won_deals)} won deal${(s.closed_won_deals || 0) === 1 ? "" : "s"}${ratio}${dashSharedExcludedNote(s)}`,
    };
  }
  if (key === "best_source") {
    return {
      value: s.label,
      line: `${fmtMoney(s.revenue_usd)} closed-won · ${s.status || "Revenue-only"}`,
    };
  }
  if (key === "biggest_waste") {
    const amount = s.currency === "USD"
      ? fmtMoney(s.value)
      : fmtCompactCurrency(s.value, s.currency || "GBP");
    return {
      value: amount,
      line: `${fmtCount(s.campaign_count)} campaign${(s.campaign_count || 0) === 1 ? "" : "s"} with spend but no SQLs · top: ${dashSharedNamed(s.top_campaign, s.top_campaign_shared_name_label)}${dashSharedIncludedNote(s)}${dashSharedExcludedNote(s)}`,
    };
  }
  // fastest_improving / biggest_decline
  const pct = s.delta_pct !== null && s.delta_pct !== undefined
    ? `${s.delta_pct > 0 ? "+" : ""}${(s.delta_pct * 100).toFixed(1)}%` : "—";
  return { value: s.label, line: `${pct} vs previous period` };
}

function renderDashSignals(d) {
  const signals = d.top_signals || {};
  const items = DASH_SIGNAL_DEFS.map(({ key, label }) => {
    const s = signals[key];
    if (!s) {
      return `
        <div class="dash-signal dash-signal--empty">
          <div class="dash-signal__label">${escapeHtml(label)}</div>
          <div class="dash-signal__line">No signal in this window</div>
        </div>`;
    }
    const body = dashSignalBody(key, s);
    const target = s.target_page ? `#/${escapeHtml(s.target_page)}` : null;
    return `
      <a class="dash-signal ${key === "biggest_waste" || key === "biggest_decline" ? "dash-signal--warn" : ""}"
         href="${target || "#"}" ${target ? "" : 'tabindex="-1"'}>
        <div class="dash-signal__label">${escapeHtml(label)}</div>
        <div class="dash-signal__value">${escapeHtml(body.value || "—")}</div>
        <div class="dash-signal__line">${escapeHtml(body.line || "")}</div>
        <span class="dash-signal__cta" aria-hidden="true">${escapeHtml(s.target_label || "View evidence")} →</span>
      </a>`;
  }).join("");

  return `
    <h3 class="dash-rail-title">Top Signals</h3>
    <div class="dash-signal-list">${items}</div>`;
}

function renderDashDecisionCards(d) {
  const cards = d.decision_cards || [];
  if (!cards.length) return "";
  return `
    <div class="dash-decision-grid">
      ${cards.map((c) => `
        <div class="dash-decision-card dash-decision-card--${escapeHtml(c.type || "scale")}">
          <div class="dash-decision-card__title">${escapeHtml(c.title || "")}</div>
          <div class="dash-decision-card__headline">${escapeHtml(c.headline || "")}</div>
          <p class="dash-decision-card__body">${escapeHtml(c.body || "")}</p>
          ${c.target_page && dashCanNavigate(c.target_page) ? `<a class="dash-decision-card__link" href="#/${escapeHtml(c.target_page)}">${escapeHtml(c.target_label || "Open evidence")} →</a>` : ""}
        </div>`).join("")}
    </div>`;
}

const DASH_STATUS_DOT = {
  ready: "dash-dot--ok",
  partial: "dash-dot--warn",
  blocked: "dash-dot--bad",
  unavailable: "dash-dot--muted",
  // PR-ADS-171B: "not set up" is a fact, not a failure to read — its own dot.
  not_connected: "dash-dot--neutral",
};

function renderDashTruthFooter(d) {
  const ts = d.truth_status || {};
  const chip = (label, status) => `
    <span class="dash-footer-chip" title="${escapeHtml(`${label}: ${status || "unknown"}`)}">
      <span class="dash-dot ${DASH_STATUS_DOT[status] || "dash-dot--muted"}"></span>${escapeHtml(label)}
    </span>`;
  const allGood = ts.overall === "ready";
  return `
    <footer class="dash-truth-footer ${allGood ? "dash-truth-footer--ok" : ""}">
      ${chip("Spend", ts.spend)}
      ${chip("Revenue", ts.revenue)}
      ${chip("FX", ts.fx)}
      ${chip("Sources", ts.source_attribution)}
      ${chip("Geo", ts.geo)}
      <span class="dash-footer-chip dash-footer-chip--readonly">Read-only</span>
      ${allGood
        ? '<span class="dash-footer-note">All truth sources verified for this window.</span>'
        : (dashCanNavigate("health")
          ? '<a class="dash-footer-note" href="#/health">Some sources need attention — open System Status</a>'
          : '<span class="dash-footer-note">Some sources need attention — contact your administrator.</span>')}
    </footer>`;
}

// ── Dashboard — Revenue & Customers tab (PR-ADS-135) ───────────────────────
//
// Second dashboard tab. Reuses the PR-134 design system end to end (command
// header, tabs, business-window selector, KPI cards, chart shell, decision
// cards, truth footer, loading/refetch/unavailable states) — NO new visual
// language. Only the Revenue content, contract, and analysis are new.
// Truth doctrine is identical: HubSpot closed-won is revenue truth, Google Ads
// conversion value is never used, unavailable data renders "Unavailable" (never
// a fake $0 / 0.00x / 0%), non-Google sources never show ROAS.

let dashRevenue = null;
let dashRevenueStatus = "loading";

const REV_ATTR_LABEL = {
  attributed: "Attributed",
  partial: "Partial",
  ambiguous: "Ambiguous",
  unattributed: "Unattributed",
};
const REV_ATTR_CLASS = {
  attributed: "rev-attr--ok",
  partial: "rev-attr--warn",
  ambiguous: "rev-attr--warn",
  unattributed: "rev-attr--muted",
};

function revAttrBadge(status) {
  const key = (status || "").toLowerCase();
  return `<span class="rev-attr ${REV_ATTR_CLASS[key] || "rev-attr--muted"}">${escapeHtml(REV_ATTR_LABEL[key] || "—")}</span>`;
}

async function loadDashboardRevenue() {
  const window_ = getRoasBusinessWindow();
  const token = ++_revReqSeq.dashRevenue;
  setWindowRangeLoading("dashboard-range");

  const root = document.getElementById("dashboard-revenue-root");
  const genEl = document.getElementById("dashboard-generated");
  if (root) {
    if (dashRevenue && dashRevenueStatus === "ok") {
      root.classList.add("is-refreshing");
    } else {
      root.innerHTML = renderDashSkeleton();
    }
  }

  try {
    const data = await fetchJSON(
      `/api/dashboard/revenue?window=${encodeURIComponent(window_)}`
    );
    if (token !== _revReqSeq.dashRevenue) return;
    if (!_revResponseIsCurrent("dashRevenue", token, data)) return;
    dashRevenue = data;
    dashRevenueStatus = "ok";
    renderWindowRange("dashboard-range", data.window || null);
    if (genEl) genEl.textContent = data.generated_at ? `Generated ${fmtDate(data.generated_at)}` : "";
    renderDashboardRevenue();
  } catch (err) {
    console.error("[loadDashboardRevenue]", err);
    if (token !== _revReqSeq.dashRevenue) return;
    dashRevenue = null;
    dashRevenueStatus = "error";
    if (genEl) genEl.textContent = "";
    renderWindowRange("dashboard-range", null);
    renderDashboardRevenue();
  }
}

function renderDashboardRevenue() {
  const root = document.getElementById("dashboard-revenue-root");
  if (!root) return;
  root.classList.remove("is-refreshing");

  if (dashRevenueStatus !== "ok" || !dashRevenue) {
    const nextStep = dashCanNavigate("health")
      ? 'Next step: check <a href="#/health">System Status</a> or retry with Refresh.'
      : "Next step: retry with Refresh, or contact your administrator.";
    root.innerHTML = `
      <div class="revenue-blocked-card dash-error-card">
        <h3 class="revenue-blocked-card__title">Revenue &amp; Customers unavailable</h3>
        <p>The dashboard revenue service could not be reached. Nothing is shown
        because nothing safe could be computed — metrics are never fabricated.</p>
        <p class="revenue-blocked-card__next">${nextStep}</p>
      </div>`;
    return;
  }

  const d = dashRevenue;
  root.innerHTML = `
    <div class="dash-tab-intro dash-anim">
      <h2 class="dash-tab-intro__title">Revenue &amp; Customers</h2>
      <p class="dash-tab-intro__sub">Closed-won revenue, customer movement, and deal proof from HubSpot truth.</p>
    </div>
    ${renderRevKpiRow(d)}
    <div class="dash-main-grid">
      <section class="dash-panel dash-chart-panel dash-anim" aria-label="Revenue momentum">
        ${renderRevMomentumPanel(d)}
      </section>
      <aside class="dash-panel dash-anim rev-concentration-panel" aria-label="Deal concentration">
        ${renderRevConcentration(d)}
      </aside>
    </div>
    ${renderRevCustomerEfficiency(d)}
    ${renderRevBreakdowns(d)}
    <section class="dash-panel dash-anim rev-deals-panel" aria-label="Top closed-won deals">
      ${renderRevTopDeals(d)}
    </section>
    <section class="dash-decisions dash-anim" aria-label="Revenue decision cards">
      ${renderDashDecisionCards(d)}
    </section>
    ${renderRevTruthFooter(d)}
  `;
  wireRevChartHover(root, d);
  wireRevDealDrawers(root, d);
}

function renderRevKpiRow(d) {
  const k = d.kpis || {};
  const pc = d.period_change || {};
  // PR-ADS-153E-B: the canonical ledger names the DEAL, not a company.
  const largestDealCompany = (d.top_deals || [])[0] && ((d.top_deals || [])[0].deal_name || (d.top_deals || [])[0].company);

  const cards = [
    {
      key: "revenue",
      label: "Closed-Won Revenue",
      value: canonicalValueHtml(k.closed_won_revenue_usd, (k.metrics || {}).revenue_usd, fmtMoney, { windowType: "business", windowKey: (d.window || {}).key }),
      sub: "USD · HubSpot closed-won",
      delta: dashDeltaChip("closed_won_revenue_usd", pc),
      source: "HubSpot Closed-Won",
      ok: k.closed_won_revenue_usd !== null && k.closed_won_revenue_usd !== undefined,
    },
    {
      key: "customers",
      label: "Customers",
      // PR-ADS-161E: distinct proven HubSpot companies, with the canonical
      // verdict — never the deal count, which is the sub-line.
      value: canonicalMetricHtml(
        { value: k.customers, status: k.customers_status, reason: k.customers_reason },
        fmtCount, { windowType: "business", windowKey: (d.window || {}).key }),
      sub: closedWonDealsSubText(k.closed_won_deals, (k.metrics || {}).closed_won_deals),
      delta: dashDeltaChip("customers", pc),
      source: "HubSpot companies behind closed-won deals",
      ok: k.customers_status === "published",
    },
    {
      key: "avg",
      label: "Average Deal Value",
      value: dashValue(k.average_deal_value_usd, fmtMoney),
      sub: "Revenue ÷ closed-won deals",
      delta: dashDeltaChip("average_deal_value_usd", pc),
      source: "HubSpot Closed-Won",
      ok: k.average_deal_value_usd !== null && k.average_deal_value_usd !== undefined,
    },
    {
      key: "largest",
      label: "Largest Deal",
      value: dashValue(k.largest_deal_usd, fmtMoney),
      // When the largest deal is Unavailable (an amount is unknown, so the
      // biggest deal can't be identified), don't name a company under it.
      sub: (k.largest_deal_usd !== null && k.largest_deal_usd !== undefined && largestDealCompany)
        ? escapeHtml(largestDealCompany) : "Single biggest closed-won deal",
      delta: '<span class="dash-delta dash-delta--none">Single deal</span>',
      source: "HubSpot Closed-Won",
      ok: k.largest_deal_usd !== null && k.largest_deal_usd !== undefined,
    },
    {
      key: "rate",
      label: "SQL → Customer Rate",
      // PR-ADS-161E: unavailable by contract — the SQLs are an acquisition
      // cohort, the won deals a close-date one; the API states why.
      value: canonicalMetricHtml(
        { value: k.sql_to_customer_rate, status: k.sql_to_customer_rate_status,
          reason: k.sql_to_customer_rate_reason }, (v) => `${(v * 100).toFixed(1)}%`),
      // PR-ADS-177A-F2 (truth audit round 2, MINOR 1): the cohort count goes
      // through the one gate (as-of note when stale, Withheld + reason) and
      // says its last day.
      sub: `${COHORT_SQLS_LABEL}${escapeHtml(cohortThroughSuffix(d.cohort))}: ${cohortCountHtml(k.sqls, d.cohort, null)}`,
      delta: '<span class="dash-delta dash-delta--none">Not a cohort rate</span>',
      source: "HubSpot acquisition cohort",
      ok: k.sql_to_customer_rate_status === "published",
    },
  ];

  const compareNote = pc.available
    ? `<p class="dash-compare-note">Change ${escapeHtml(pc.label || "vs previous period")}${pc.note ? ` — ${escapeHtml(pc.note)}` : ""}</p>`
    : `<p class="dash-compare-note dash-compare-note--muted">No previous-period comparison${pc.reason ? ` — ${escapeHtml(pc.reason)}` : ""}</p>`;

  return `
    <div class="dash-kpi-grid">
      ${cards.map((c) => `
        <div class="dash-kpi-card dash-anim ${c.ok ? "" : "dash-kpi-card--unavailable"}" data-kpi="${c.key}">
          <div class="dash-kpi-card__label">${escapeHtml(c.label)}</div>
          <div class="dash-kpi-card__value">${c.value}</div>
          <div class="dash-kpi-card__sub">${c.sub}</div>
          <div class="dash-kpi-card__meta">${c.delta}</div>
          <div class="dash-kpi-card__source">${escapeHtml(c.source)}</div>
        </div>`).join("")}
    </div>
    ${compareNote}`;
}

function renderRevMomentumPanel(d) {
  const trend = d.revenue_trend || {};
  const k = d.kpis || {};
  const bucketLabel = trend.bucket === "month" ? "Monthly" : (trend.bucket === "week" ? "Weekly" : "");
  const note = trend.status !== "ready"
    ? `<p class="dash-series-note">Revenue series unavailable — ${escapeHtml(trend.reason || "not verified")}</p>` : "";

  return `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Revenue Momentum</h3>
        <p class="dash-panel__sub">More won deals, or one big deal?${bucketLabel ? ` · ${bucketLabel}` : ""}</p>
      </div>
      <div class="dash-legend">
        <span class="dash-legend__item"><span class="dash-legend__swatch" style="background:${DASH_SERIES_COLORS.revenue}"></span>Revenue</span>
        <span class="dash-legend__item"><span class="rev-legend-badge">N</span>Won deals / period</span>
      </div>
    </div>
    ${note}
    ${revMomentumChartSVG(trend)}
    <p class="dash-chart-caption">
      Window total revenue: ${canonicalValueHtml(k.closed_won_revenue_usd, (k.metrics || {}).revenue_usd, fmtMoney)} across
      ${dashValue(k.closed_won_deals, fmtCount)} closed-won deals · Avg ${dashValue(k.average_deal_value_usd, fmtMoney)}
    </p>`;
}

// Revenue bars with the customer COUNT as a direct label on each bar cap — NOT
// a second y-axis (dual-axis is a dataviz anti-pattern). The bar height reads
// revenue; the small count answers "one big deal or many customers?".
function revMomentumChartSVG(trend) {
  const points = (trend && trend.points) || [];
  if (trend.status !== "ready" || !points.length) {
    return '<div class="dash-chart-empty">No chartable revenue in this window.</div>';
  }
  const W = 760, H = 240, padL = 52, padR = 14, padT = 18, padB = 30;
  const plotW = W - padL - padR, plotH = H - padT - padB, n = points.length;
  const slot = plotW / n, barW = Math.min(24, slot * 0.55);
  const values = points.map((p) => p.revenue_usd || 0);
  const yMax = dashNiceCeil(Math.max(1, ...values));
  const xCenter = (i) => padL + slot * i + slot / 2;
  const yPos = (v) => padT + plotH * (1 - v / yMax);
  const baseline = padT + plotH;

  const ticks = [0.25, 0.5, 0.75, 1];
  const grid = ticks.map((t) => {
    const y = yPos(yMax * t).toFixed(1);
    return `<line x1="${padL}" x2="${W - padR}" y1="${y}" y2="${y}" stroke="#e0e6ef" stroke-width="1"/>
      <text x="${padL - 8}" y="${Number(y) + 3}" text-anchor="end" class="dash-chart-tick">${dashCompactMoney(yMax * t)}</text>`;
  }).join("");

  const bars = points.map((p, i) => {
    const hasDeals = (p.deals !== null && p.deals !== undefined && p.deals > 0);
    const v = p.revenue_usd || 0;
    const h = Math.max(0, baseline - yPos(v));
    const x = xCenter(i) - barW / 2, y = yPos(v), r = Math.min(4, barW / 2, h || 4);
    // A bucket with won deals but null/zero summed revenue (a permitted null
    // amount — never faked as $0) still shows its deal count at the
    // baseline, so real deal activity is never hidden from the chart.
    if (h === 0) {
      return hasDeals
        ? `<text x="${xCenter(i).toFixed(1)}" y="${(baseline - 6).toFixed(1)}" text-anchor="middle" class="rev-count-label">${escapeHtml(String(p.deals))}</text>` : "";
    }
    const countLabel = hasDeals
      ? `<text x="${xCenter(i).toFixed(1)}" y="${(y - 6).toFixed(1)}" text-anchor="middle" class="rev-count-label">${escapeHtml(String(p.deals))}</text>` : "";
    return `<path d="M${x.toFixed(1)},${baseline.toFixed(1)}
      L${x.toFixed(1)},${(y + r).toFixed(1)}
      Q${x.toFixed(1)},${y.toFixed(1)} ${(x + r).toFixed(1)},${y.toFixed(1)}
      L${(x + barW - r).toFixed(1)},${y.toFixed(1)}
      Q${(x + barW).toFixed(1)},${y.toFixed(1)} ${(x + barW).toFixed(1)},${(y + r).toFixed(1)}
      L${(x + barW).toFixed(1)},${baseline.toFixed(1)} Z" fill="${DASH_SERIES_COLORS.revenue}" fill-opacity="0.92"/>${countLabel}`;
  }).join("");

  const labelEvery = Math.max(1, Math.ceil(n / 6));
  const xLabels = points.map((p, i) => {
    if (i !== 0 && i !== n - 1) {
      if (i % labelEvery !== 0) return "";
      if (n - 1 - i < Math.max(2, Math.ceil(labelEvery * 0.6))) return "";
    }
    return `<text x="${xCenter(i).toFixed(1)}" y="${H - 8}" text-anchor="middle" class="dash-chart-tick">${escapeHtml(p.period_label || "")}</text>`;
  }).join("");

  const hitBands = points.map((p, i) =>
    `<rect class="dash-chart-hit" data-dash-idx="${i}" x="${(padL + slot * i).toFixed(1)}" y="${padT}"
      width="${slot.toFixed(1)}" height="${plotH}" fill="transparent"/>`).join("");

  return `
    <div class="dash-chart-wrap">
      <svg viewBox="0 0 ${W} ${H}" class="dash-combo-chart" role="img"
        aria-label="Closed-won revenue bars per ${trend.bucket || "period"} with customer counts">
        ${grid}
        <line x1="${padL}" x2="${W - padR}" y1="${baseline}" y2="${baseline}" stroke="#cbd5e1" stroke-width="1"/>
        ${bars}
        ${xLabels}
        ${hitBands}
      </svg>
      <div class="dash-chart-tooltip" hidden></div>
    </div>`;
}

function wireRevChartHover(root, d) {
  const wrap = root.querySelector(".dash-chart-wrap");
  if (!wrap) return;
  const tooltip = wrap.querySelector(".dash-chart-tooltip");
  const points = ((d.revenue_trend || {}).points) || [];

  function hide() {
    if (tooltip) tooltip.hidden = true;
    wrap.querySelectorAll(".dash-chart-hit.is-hover").forEach((el) => el.classList.remove("is-hover"));
  }
  wrap.addEventListener("pointermove", (e) => {
    const band = e.target.closest(".dash-chart-hit");
    if (!band || !tooltip) { hide(); return; }
    const p = points[Number(band.dataset.dashIdx)];
    if (!p) { hide(); return; }
    wrap.querySelectorAll(".dash-chart-hit.is-hover").forEach((el) => el.classList.remove("is-hover"));
    band.classList.add("is-hover");
    tooltip.textContent = "";
    const title = document.createElement("div");
    title.className = "dash-chart-tooltip__title";
    title.textContent = p.period_label || "";
    tooltip.appendChild(title);
    [["Revenue", p.revenue_usd === null || p.revenue_usd === undefined ? "Unavailable" : fmtMoney(p.revenue_usd), DASH_SERIES_COLORS.revenue],
     ["Won deals", p.deals === null || p.deals === undefined ? "Unavailable" : String(p.deals), null]].forEach(([label, val, color]) => {
      const row = document.createElement("div");
      row.className = "dash-chart-tooltip__row";
      if (color) {
        const key = document.createElement("span");
        key.className = "dash-chart-tooltip__key";
        key.style.background = color;
        row.appendChild(key);
      }
      const strong = document.createElement("strong");
      strong.textContent = val;
      const name = document.createElement("span");
      name.textContent = ` ${label}`;
      row.appendChild(strong);
      row.appendChild(name);
      tooltip.appendChild(row);
    });
    const wrapRect = wrap.getBoundingClientRect();
    const bandRect = band.getBoundingClientRect();
    tooltip.hidden = false;
    const tipW = tooltip.offsetWidth || 140;
    let left = bandRect.left - wrapRect.left + bandRect.width / 2 - tipW / 2;
    left = Math.max(4, Math.min(left, wrapRect.width - tipW - 4));
    tooltip.style.left = `${left}px`;
    tooltip.style.top = "8px";
  });
  wrap.addEventListener("pointerleave", hide);
}

// PR-ADS-180A — "Customer Efficiency" is NOT a lifecycle funnel: its SQL count
// is the acquisition cohort, its deals and revenue are counted by close date. It
// was drawn as a stage strip with arrows between those two populations; it is
// now the Dashboard's outcome cells, side by side with no connector, and its
// two cross-cohort ratios are their own labelled cells (unavailable by
// contract, with the reason). Every value renders through the same renderer
// as before.
function renderRevCustomerEfficiency(d) {
  const k = d.kpis || {};
  const cells = [
    // PR-ADS-177A: the acquisition cohort, gated on THIS response's cohort.
    { label: COHORT_SQLS_LABEL, value: cohortCountHtml(k.sqls, d.cohort, null),
      sub: `Google Ads contacts${cohortThroughSuffix(d.cohort)}` },
    // PR-ADS-161E: both ratios are unavailable by contract — SQLs are an
    // acquisition cohort, won deals a close-date one — and say why.
    { label: "SQL-to-customer rate", value: canonicalMetricHtml(
      { value: k.sql_to_customer_rate, status: k.sql_to_customer_rate_status,
        reason: k.sql_to_customer_rate_reason }, (v) => `${(v * 100).toFixed(1)}%`),
      sub: "Across two populations" },
    { label: "Closed-Won Deals", value: dashValue(k.closed_won_deals, fmtCount), sub: "By close date" },
    { label: "Closed-Won Revenue", value: canonicalValueHtml(k.closed_won_revenue_usd, (k.metrics || {}).revenue_usd, fmtMoney, { windowType: "business", windowKey: (d.window || {}).key }), sub: "USD · HubSpot" },
    { label: "Revenue per SQL", value: canonicalMetricHtml(
      { value: k.revenue_per_sql_usd, status: k.revenue_per_sql_status,
        reason: k.revenue_per_sql_reason }, (v) => `${fmtMoney(v)} / SQL`),
      sub: "Across two populations" },
    { label: "Avg Deal Value", value: dashValue(k.average_deal_value_usd, fmtMoney), sub: "Per won deal" },
  ];
  return `
    <section class="dash-panel dash-outcomes-panel dash-anim rev-efficiency-panel" aria-label="Customer efficiency">
      <div class="dash-panel__header">
        <div>
          <h3 class="dash-panel__title">Customer Efficiency</h3>
          <p class="dash-panel__sub">SQLs are contacts acquired in this window; deals and revenue are counted by close date. Two populations, so no arrow or rate joins them.</p>
        </div>
      </div>
      <div class="dash-outcomes">${cells.map((c) => `
        <div class="dash-outcomes__cell">
          <div class="dash-outcomes__label">${escapeHtml(c.label)}</div>
          <div class="dash-outcomes__value">${c.value}</div>
          <div class="dash-outcomes__sub">${escapeHtml(c.sub)}</div>
        </div>`).join("")}
      </div>
    </section>`;
}

function renderRevBreakdownPanel(title, sub, rows, nameKey, targetPage, opts) {
  opts = opts || {};
  // PR-ADS-161E: "no attributed revenue" is a claim about a PUBLISHED window;
  // a withheld or unavailable one says so, with the canonical reason.
  const windowDeals = opts.windowDeals || {};
  const emptyHtml = (windowDeals.status && windowDeals.status !== "published")
    ? `<p class="rev-breakdown-empty">Closed-won deals: ${canonicalMetricHtml(windowDeals, fmtCount, opts.blockerOpts)}</p>`
    : '<p class="rev-breakdown-empty">No attributed revenue in this window.</p>';
  const body = !rows || !rows.length
    ? emptyHtml
    : `<ul class="rev-breakdown-list">${rows.map((r) => {
        const extra = opts.spendConnected
          ? `<span class="rev-breakdown-row__status">${escapeHtml(r.status || "")}</span>`
          : revAttrBadge(r.attribution_status);
        // PR-ADS-161E: deals, never "customers" — a group's customer count
        // (distinct companies) is not additive and is not shown per row here.
        const custDeals = opts.spendConnected
          ? `${rowMetricHtml(r, "closed_won_deals", fmtCount, opts.blockerOpts)} won`
          : `${rowMetricHtml(r, "deals", fmtCount, opts.blockerOpts)} deals`;
        return `
        <li class="rev-breakdown-row">
          <span class="rev-breakdown-row__name" title="${escapeHtml(r[nameKey] || "")}">${escapeHtml(r[nameKey] || "—")}</span>
          <span class="rev-breakdown-row__value">${rowMetricHtml(r, "revenue_usd", fmtMoney, opts.blockerOpts)}</span>
          <span class="rev-breakdown-row__meta">${custDeals}</span>
          ${extra}
        </li>`;
      }).join("")}</ul>`;
  const link = dashCanNavigate(targetPage)
    ? `<a class="rev-breakdown-link" href="#/${escapeHtml(targetPage)}">${escapeHtml(opts.linkLabel || "Open evidence")} →</a>`
    : '<span class="rev-breakdown-link rev-breakdown-link--muted">Admin only</span>';
  return `
    <div class="dash-panel rev-breakdown-panel dash-anim">
      <div class="dash-panel__header">
        <div>
          <h3 class="dash-panel__title">${escapeHtml(title)}</h3>
          <p class="dash-panel__sub">${escapeHtml(sub)}</p>
        </div>
      </div>
      ${body}
      ${link}
    </div>`;
}

function renderRevBreakdowns(d) {
  const b = d.breakdowns || {};
  const common = {
    windowDeals: (((d.kpis || {}).metrics) || {}).closed_won_deals,
    blockerOpts: { windowType: "business", windowKey: (d.window || {}).key },
  };
  return `
    <div class="rev-breakdown-grid">
      ${renderRevBreakdownPanel("Revenue by Campaign", "Top 5 by closed-won revenue",
        b.by_campaign, "campaign", "campaigns", common)}
      ${renderRevBreakdownPanel("Closed-won by source group · this window", "Top 5 · Google Ads spend-connected only",
        b.by_source, "source_group", "channels",
        // PR-ADS-168B review: Channels uses evidence windows grouped by HubSpot
        // original source, so the link must not promise these figures there.
        { ...common, spendConnected: true, linkLabel: CHANNELS_LINK_LABEL })}
      ${renderRevBreakdownPanel("Revenue by Country", "Top 5 by closed-won revenue",
        b.by_country, "country", "roas-countries", common)}
    </div>`;
}

function renderRevConcentration(d) {
  const c = d.deal_concentration;
  if (!c) {
    return `
      <div class="dash-panel__header">
        <div><h3 class="dash-panel__title">Deal Concentration</h3></div>
      </div>
      <p class="rev-breakdown-empty">Deal concentration is unavailable for this window.</p>`;
  }
  const verdictClass = c.label === "Highly concentrated" ? "rev-conc--high"
    : (c.label === "Moderately concentrated" ? "rev-conc--mid" : "rev-conc--ok");
  const bar = (share) => `
    <div class="rev-conc-bar"><div class="rev-conc-bar__fill" style="width:${Math.round((share || 0) * 100)}%"></div></div>`;
  return `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Deal Concentration</h3>
        <p class="dash-panel__sub">Is revenue spread or lumpy?</p>
      </div>
    </div>
    <div class="rev-conc">
      <div class="rev-conc-verdict ${verdictClass}">${escapeHtml(c.label)}</div>
      <div class="rev-conc-metric">
        <span class="rev-conc-metric__label">Top deal</span>
        <span class="rev-conc-metric__val">${Math.round((c.top_1_deal_share || 0) * 100)}%</span>
        ${bar(c.top_1_deal_share)}
      </div>
      <div class="rev-conc-metric">
        <span class="rev-conc-metric__label">Top 3 deals</span>
        <span class="rev-conc-metric__val">${Math.round((c.top_3_deals_share || 0) * 100)}%</span>
        ${bar(c.top_3_deals_share)}
      </div>
      <p class="rev-conc-foot">${dashValue(c.deals, fmtCount)} closed-won deals this window</p>
    </div>`;
}

const REV_DEAL_COLUMNS = [
  ["deal_name", "Deal name"], ["company", "Company"], ["company_id", "Company ID"], ["main_contact", "Main Contact"],
  ["contact_id", "Contact ID"], ["deal", "Deal"], ["deal_id", "Deal ID"],
  ["amount_usd", "Amount"], ["close_date", "Close Date"], ["campaign", "Campaign"],
  ["source", "Source"], ["country", "Country"], ["attribution_status", "Attribution"],
];

function revDealCell(deal, key) {
  if (key === "amount_usd") return dashValue(deal.amount_usd, fmtMoney); // null → Unavailable, never $0
  if (key === "attribution_status") return revAttrBadge(deal.attribution_status);
  return dashValue(deal[key]);
}

function renderRevTopDeals(d) {
  const deals = d.top_deals || [];
  if (!deals.length) {
    return `
      <div class="dash-panel__header">
        <div><h3 class="dash-panel__title">Top Deals — Proof</h3></div>
      </div>
      <p class="rev-breakdown-empty">No closed-won deals to prove revenue in this window.</p>`;
  }
  const head = REV_DEAL_COLUMNS.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("");
  const body = deals.map((deal, i) => {
    const cells = REV_DEAL_COLUMNS.map(([key]) => `<td class="rev-deal-td rev-deal-td--${key}">${revDealCell(deal, key)}</td>`).join("");
    return `<tr class="rev-deal-row" data-rev-deal="${i}" tabindex="0" role="button" aria-label="Open deal detail">${cells}</tr>`;
  }).join("");
  return `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Top Deals — Proof</h3>
        <p class="dash-panel__sub">The closed-won deals behind this window's revenue · click a row for detail</p>
      </div>
    </div>
    <div class="rev-deal-scroll">
      <table class="rev-deal-table">
        <thead><tr>${head}</tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>`;
}

function wireRevDealDrawers(root, d) {
  const deals = d.top_deals || [];
  const open = (i) => {
    const deal = deals[i];
    if (!deal) return;
    const rows = [
      ["Deal name", dashValue(deal.deal_name)],
      ["Company", dashValue(deal.company)], ["Company ID", dashValue(deal.company_id)],
      ["Main Contact", dashValue(deal.main_contact)], ["Contact ID", dashValue(deal.contact_id)],
      ["Deal", dashValue(deal.deal)], ["Deal ID", dashValue(deal.deal_id)],
      ["Amount", dashValue(deal.amount_usd, fmtMoney)], ["Close Date", dashValue(deal.close_date)],
      ["Source", dashValue(deal.source)], ["Campaign", dashValue(deal.campaign)],
      ["Country", dashValue(deal.country)], ["Attribution", revAttrBadge(deal.attribution_status)],
      ["Attribution reason", dashValue(deal.attribution_reason)],
    ];
    let drawer = root.querySelector(".rev-deal-drawer");
    if (!drawer) {
      drawer = document.createElement("div");
      drawer.className = "rev-deal-drawer";
      root.querySelector(".rev-deals-panel").appendChild(drawer);
    }
    drawer.innerHTML = `
      <div class="rev-deal-drawer__head">
        <strong>${escapeHtml(deal.deal_name || deal.company || "Deal detail")}</strong>
        <button type="button" class="rev-deal-drawer__close" aria-label="Close">×</button>
      </div>
      <dl class="rev-deal-drawer__grid">
        ${rows.map(([label, val]) => `<div><dt>${escapeHtml(label)}</dt><dd>${val}</dd></div>`).join("")}
      </dl>`;
    drawer.querySelector(".rev-deal-drawer__close").addEventListener("click", () => drawer.remove());
    drawer.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  root.querySelectorAll(".rev-deal-row").forEach((tr) => {
    tr.addEventListener("click", () => open(Number(tr.dataset.revDeal)));
    tr.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(Number(tr.dataset.revDeal)); }
    });
  });
}

function renderRevTruthFooter(d) {
  const ts = d.truth_status || {};
  const chip = (label, status) => `
    <span class="dash-footer-chip" title="${escapeHtml(`${label}: ${status || "unknown"}`)}">
      <span class="dash-dot ${DASH_STATUS_DOT[status] || "dash-dot--muted"}"></span>${escapeHtml(label)}
    </span>`;
  const allGood = ts.revenue === "ready" && ts.deal_ledger === "ready"
    && ts.source_attribution === "ready" && ts.campaign_attribution === "ready"
    && ts.country_attribution === "ready";
  return `
    <footer class="dash-truth-footer ${allGood ? "dash-truth-footer--ok" : ""}">
      ${chip("Revenue ledger", ts.deal_ledger)}
      ${chip("HubSpot", ts.hubspot_integration)}
      ${chip("Source attr.", ts.source_attribution)}
      ${chip("Campaign attr.", ts.campaign_attribution)}
      ${chip("Country attr.", ts.country_attribution)}
      <span class="dash-footer-chip dash-footer-chip--readonly">Read-only</span>
      ${allGood
        ? '<span class="dash-footer-note">All revenue truth verified for this window.</span>'
        : (dashCanNavigate("revenue-health")
          ? '<a class="dash-footer-note" href="#/revenue-health">Some attribution needs review — open Revenue Health</a>'
          : '<span class="dash-footer-note">Some attribution needs review — contact your administrator.</span>')}
    </footer>`;
}

// ── Dashboard — Channels & Platforms tab: retired (PR-ADS-181A) ───────────
// The third Dashboard tab (PR-ADS-136, /api/dashboard/channels) is retired by
// owner decision: the Channels page (#/channels, /api/channel-funnel) is the
// one channel surface. A bookmarked #/dashboard?tab=channels opens it.

// PR-ADS-177A-F1 (truth audit MAJOR 2): the LEGACY lead-status count
// (`leads.status_category = 'qualified'`) — a different metric from the
// cohort's SQLs, never shown under the bare word "SQL" / "SQLs". (Revenue
// Health's mapping cards read it.)
const LEAD_STATUS_QUALIFIED_LABEL = "Qualified (lead status)";

// ── Dashboard — Campaigns & Keywords tab (PR-ADS-137) ──────────────────────
//
// Fourth dashboard tab. Reuses the PR-134/135/136 design system end to end (KPI
// cards, chart shell, decision cards, truth footer, loading/refetch/unavailable
// states) — NO new visual language. It answers: which Google Ads campaigns and
// keyword/search-intent themes produce real business outcomes, and which spend
// without proof? Truth doctrine is identical: Google Ads spend is native GBP
// (never labelled USD); HubSpot closed-won is revenue truth (USD); campaign ROAS
// only when spend + FX + revenue are safe; keyword themes carry NO outcome
// attribution and NO ROAS; search-term panels are read-only evidence.

let dashCampaigns = null;
let dashCampaignsStatus = "loading";

// Campaign status → colour + dot class (bubble map + badges).
const CAMP_STATUS_META = {
  "Revenue proven": { color: "#22b07d", dot: "camp-dot--proven" },
  "SQL producer": { color: "#129ef5", dot: "camp-dot--sql" },
  "Spend without SQL / customer proof": { color: "#ff8a3d", dot: "camp-dot--waste" },
  "Revenue attribution missing": { color: "#7c5cff", dot: "camp-dot--attr" },
  "FX unavailable": { color: "#8896a6", dot: "camp-dot--muted" },
  "Mapping needs review": { color: "#b6bfca", dot: "camp-dot--muted" },
  // PR-ADS-161E: the campaign's closed-won deals are withheld — not judged.
  "Closed-won outcome withheld": { color: "#8896a6", dot: "camp-dot--muted" },
  // PR-ADS-177A: the acquisition cohort is withheld — not judged on SQLs.
  "SQLs withheld": { color: "#8896a6", dot: "camp-dot--muted" },
  // PR-ADS-177B: the window's spend is not held completely yet.
  "Spend not settled": { color: "#8896a6", dot: "camp-dot--muted" },
};
function campStatusColor(status) {
  return (CAMP_STATUS_META[status] || { color: "#b6bfca" }).color;
}

async function loadDashboardCampaigns() {
  const window_ = getRoasBusinessWindow();
  const token = ++_revReqSeq.dashCampaigns;
  setWindowRangeLoading("dashboard-range");

  const root = document.getElementById("dashboard-campaigns-root");
  const genEl = document.getElementById("dashboard-generated");
  if (root) {
    if (dashCampaigns && dashCampaignsStatus === "ok") {
      root.classList.add("is-refreshing");
    } else {
      root.innerHTML = renderDashSkeleton();
    }
  }

  try {
    const data = await fetchJSON(
      `/api/dashboard/campaigns?window=${encodeURIComponent(window_)}`
    );
    if (token !== _revReqSeq.dashCampaigns) return;
    if (!_revResponseIsCurrent("dashCampaigns", token, data)) return;
    dashCampaigns = data;
    dashCampaignsStatus = "ok";
    renderWindowRange("dashboard-range", data.window || null);
    if (genEl) genEl.textContent = data.generated_at ? `Generated ${fmtDate(data.generated_at)}` : "";
    renderDashboardCampaigns();
  } catch (err) {
    console.error("[loadDashboardCampaigns]", err);
    if (token !== _revReqSeq.dashCampaigns) return;
    dashCampaigns = null;
    dashCampaignsStatus = "error";
    if (genEl) genEl.textContent = "";
    renderWindowRange("dashboard-range", null);
    renderDashboardCampaigns();
  }
}

function renderDashboardCampaigns() {
  const root = document.getElementById("dashboard-campaigns-root");
  if (!root) return;
  root.classList.remove("is-refreshing");

  if (dashCampaignsStatus !== "ok" || !dashCampaigns) {
    const nextStep = dashCanNavigate("health")
      ? 'Next step: check <a href="#/health">System Status</a> or retry with Refresh.'
      : "Next step: retry with Refresh, or contact your administrator.";
    root.innerHTML = `
      <div class="revenue-blocked-card dash-error-card">
        <h3 class="revenue-blocked-card__title">Campaigns unavailable</h3>
        <p>The dashboard campaigns service could not be reached. Nothing is shown
        because nothing safe could be computed — metrics are never fabricated.</p>
        <p class="revenue-blocked-card__next">${nextStep}</p>
      </div>`;
    return;
  }

  const d = dashCampaigns;
  // PR-ADS-177B: the settled campaign view — no completed day yet means
  // nothing was measured: the disclosure only.
  if (windowHasNoSettledDay(d.window)) {
    root.innerHTML = renderNoSettledDayCard(d.window);
    return;
  }
  const through = windowThroughNote(d.window);
  root.innerHTML = `
    <div class="dash-tab-intro dash-anim">
      <h2 class="dash-tab-intro__title">Campaigns</h2>
      <p class="dash-tab-intro__sub">Which Google Ads campaigns produce SQLs, customers and closed-won revenue — and which spend without proof. Spend is Google Ads (native GBP); revenue is HubSpot closed-won (USD).${through ? ` ${escapeHtml(through)}` : ""}</p>
    </div>
    ${renderCampKpiRow(d)}
    <section class="dash-panel dash-chart-panel dash-anim camp-map-panel" aria-label="Campaign performance map">
      ${renderCampPerformanceMap(d)}
    </section>
    <section class="dash-panel dash-anim camp-leaderboard-panel" aria-label="Campaign leaderboard">
      ${renderCampLeaderboard(d)}
    </section>
    <section class="dash-panel dash-anim camp-search-panel" aria-label="Search term signals">
      ${renderCampSearchSignals(d)}
    </section>
    <section class="dash-decisions dash-anim" aria-label="Campaign decision cards">
      ${renderDashDecisionCards(d)}
    </section>
    ${renderCampTruthFooter(d)}
  `;
  wireCampBubbleHover(root, d);
  wireCampDrawers(root, d);
}

// Native GBP spend cell (never a "$"), with USD appended only when FX-safe.
function campSpendCell(row) {
  if (row.spend_native === null || row.spend_native === undefined) {
    // PR-ADS-177B: a spend the settle rule withholds names its gap.
    const why = canonicalReasonLabel(row.spend_reason);
    return `<span class="dash-unavailable"${why ? ` title="${escapeHtml(why)}"` : ""}>Unavailable</span>${why ? ` <small class="metric-reason">${escapeHtml(why)}</small>` : ""}`;
  }
  const native = escapeHtml(fmtCompactCurrency(row.spend_native, row.native_currency || "GBP"));
  if (row.spend_usd !== null && row.spend_usd !== undefined) {
    return `${native} <span class="camp-usd">· ${escapeHtml(fmtMoney(row.spend_usd))}</span>`;
  }
  return native;
}

// PR-ADS-177C-F1 (truth audit M-1). A campaign row whose name several Google
// Ads campaign ids share (identity_status) is the TRUE combined figure for
// that name — never one campaign's. Every surface that prints such a row says
// so in plain words beside the name and the period ratio, and its drawer
// lists the ids. The figures are not withheld; they are labelled.
const CAMPAIGN_SHARED_NAME = "campaign_name_shared_by_google_ads_campaigns";
function campaignSharedCount(r) {
  if (!r || r.identity_status !== CAMPAIGN_SHARED_NAME) return 0;
  const ids = Array.isArray(r.google_ads_campaign_ids) ? r.google_ads_campaign_ids : [];
  return ids.length > 1 ? ids.length : -1;          // -1: shared, count unknown
}
function campaignSharedText(r) {
  const n = campaignSharedCount(r);
  if (!n) return "";
  return `${n > 0 ? n : "Several"} Google Ads campaigns share this name — figures combined`;
}
function campaignSharedNameHtml(r) {
  const t = campaignSharedText(r);
  return t ? `<div class="camp-shared-name" data-shared-name>${escapeHtml(t)}</div>` : "";
}
function campaignSharedRatioNote(r) {
  return campaignSharedCount(r)
    ? `<small class="metric-reason camp-shared-ratio" data-shared-name>combined across the campaigns sharing this name</small>` : "";
}
function campaignSharedIdsHtml(r) {
  const t = campaignSharedText(r);
  if (!t) return "";
  const ids = (r.google_ads_campaign_ids || []).map((id) => `<code>${escapeHtml(String(id))}</code>`).join(", ");
  return `<div class="camp-drawer__section camp-shared-ids" data-shared-name>
      <p><strong>${escapeHtml(t)}.</strong> ${ids ? `Google Ads campaign ids: ${ids}.` : ""}
      HubSpot records the campaign by name, so these figures cannot be split by
      campaign and are not judged as one campaign.</p>
    </div>`;
}

function campRoasCell(row) {
  // The period ratio only when it is a real, trusted value; else the status
  // conveys the outcome. Never labelled ROAS: a non-cohort operational ratio.
  if (row.period_ratio_available && row.period_revenue_per_ad_spend !== null
      && row.period_revenue_per_ad_spend !== undefined) {
    return `<span class="camp-roas" title="${escapeHtml(PERIOD_RATIO_DISCLOSURE)}">${escapeHtml(fmtPeriodRatio(row.period_revenue_per_ad_spend))}</span>`;
  }
  return `<span class="camp-status" style="--camp-accent:${campStatusColor(row.status)}">${escapeHtml(row.status || "—")}</span>`;
}

// Compact native-currency magnitude for the hero value (symbol + magnitude, no
// trailing code word) so a long "£10.0k GBP" never truncates. The £ symbol makes
// the currency unambiguous; the currency code is restated in the sub line.
function campNativeBig(native) {
  if (!native || native.amount === null || native.amount === undefined) {
    return '<span class="dash-unavailable">Unavailable</span>';
  }
  const sym = currencySymbol(native.currency || "GBP");
  const abs = Math.abs(native.amount);
  let body;
  if (abs >= 1e6) body = (native.amount / 1e6).toFixed(1) + "M";
  else if (abs >= 1e3) body = (native.amount / 1e3).toFixed(1) + "k";
  else body = Number(native.amount).toFixed(0);
  return escapeHtml(`${sym}${body}`);
}

function renderCampKpiRow(d) {
  const k = d.kpis || {};
  const pc = d.period_change || {};
  const native = k.verified_spend_native || {};
  const nativeVal = campNativeBig(native);
  const cur = escapeHtml(native.currency || "GBP");
  const usdSub = (k.verified_spend_usd !== null && k.verified_spend_usd !== undefined)
    ? `${cur} native · ${escapeHtml(fmtMoney(k.verified_spend_usd))} USD`
    : `${cur} native · USD unavailable (FX)`;

  const cards = [
    {
      key: "spend", label: "Verified Google Ads Spend",
      value: nativeVal, sub: usdSub,
      delta: dashDeltaChip("verified_spend_usd", pc),
      source: "Google Ads · native GBP",
      ok: native.amount !== null && native.amount !== undefined,
    },
    {
      // PR-ADS-177A: the acquisition cohort, gated on THIS response's cohort.
      key: "sqls", label: COHORT_SQLS_LABEL,
      value: cohortCountHtml(k.sqls, d.cohort, null),
      sub: (k.sqls_residual === null || k.sqls_residual === undefined)
        ? `Google Ads contacts · this window${cohortThroughSuffix(d.cohort)}`
        : `Google Ads contacts · ${cohortCountHtml(k.sqls_residual, d.cohort, null)} on no campaign row${cohortThroughSuffix(d.cohort)}`,
      delta: cohortDeltaChip("sqls", pc, d),
      source: "HubSpot acquisition cohort",
      ok: cohortCountPublication(d.cohort).publish && k.sqls !== null && k.sqls !== undefined,
    },
    {
      // PR-ADS-161E: distinct proven companies behind the campaign-attributable
      // won deals — the mart's canonical metric, not a sum of campaign rows.
      key: "customers", label: "Customers",
      value: canonicalMetricHtml(
        k.customers_metric || { value: k.customers, status: k.customers === null || k.customers === undefined ? "unavailable" : "published" },
        fmtCount, campBlockerOpts(d)),
      sub: `${closedWonDealsSubText(k.closed_won_deals, k.deals_metric)} · campaign-attributed`,
      delta: dashDeltaChip("customers", pc),
      source: "HubSpot companies on closed-won deals",
      ok: (k.customers_metric || {}).status === "published",
    },
    {
      key: "revenue", label: "Won Revenue",
      value: canonicalValueHtml(k.won_revenue_usd, k.revenue_metric, fmtMoney, { windowType: "business", windowKey: (d.window || {}).key }), sub: "USD · HubSpot closed-won",
      delta: dashDeltaChip("won_revenue_usd", pc),
      source: "HubSpot Closed-Won",
      ok: k.won_revenue_usd !== null && k.won_revenue_usd !== undefined,
    },
    {
      key: "period_revenue_per_ad_spend", label: PERIOD_RATIO_LABEL,
      // PR-ADS-177B: the mart's campaign-attributable revenue over campaign
      // spend (derive_period_revenue_per_ad_spend) — never recomputed here.
      value: (k.metrics || {}).period_revenue_per_ad_spend
        ? canonicalMetricHtml(k.metrics.period_revenue_per_ad_spend, fmtPeriodRatio, campBlockerOpts(d))
        : canonicalValueHtml(k.period_revenue_per_ad_spend, null, fmtPeriodRatio),
      // Plain-text sub (the card template escapes it) — never embed dashValue HTML.
      sub: `${(k.campaigns_with_spend === null || k.campaigns_with_spend === undefined) ? "—" : fmtCount(k.campaigns_with_spend)} campaigns with spend · ${PERIOD_RATIO_DISCLOSURE}`,
      delta: dashDeltaChip("period_revenue_per_ad_spend", pc),
      source: "Campaign-attributable revenue closed this window ÷ campaign spend, same days",
      ok: k.period_revenue_per_ad_spend !== null && k.period_revenue_per_ad_spend !== undefined,
    },
  ];

  const compareNote = pc.available
    ? `<p class="dash-compare-note">Change ${escapeHtml(pc.label || "vs previous period")}${pc.note ? ` — ${escapeHtml(pc.note)}` : ""}</p>`
    : `<p class="dash-compare-note dash-compare-note--muted">No previous-period comparison${pc.reason ? ` — ${escapeHtml(pc.reason)}` : ""}</p>`;

  return `
    <div class="dash-kpi-grid">
      ${cards.map((c) => `
        <div class="dash-kpi-card dash-anim ${c.ok ? "" : "dash-kpi-card--unavailable"}" data-kpi="${c.key}">
          <div class="dash-kpi-card__label">${escapeHtml(c.label)}</div>
          <div class="dash-kpi-card__value">${c.value}</div>
          <div class="dash-kpi-card__sub">${escapeHtml(c.sub)}</div>
          <div class="dash-kpi-card__meta">${c.delta}</div>
          <div class="dash-kpi-card__source">${escapeHtml(c.source)}</div>
        </div>`).join("")}
    </div>
    ${compareNote}`;
}

// Campaign performance bubble map: x = native spend (GBP), y = customers, size = SQLs.
// PR-ADS-161E: a campaign whose closed-won deals are withheld has no y value —
// plotting it at 0 would draw "spend, no deals". It is left off the map and
// counted beside it instead.
function campMapRows(d) {
  return (d.campaigns || []).filter((r) =>
    r.spend_native !== null && r.spend_native !== undefined && r.spend_native > 0
    && r.closed_won_deals !== null && r.closed_won_deals !== undefined);
}

function renderCampPerformanceMap(d) {
  const rows = campMapRows(d);
  const withheldN = (d.campaigns || []).filter((r) => (r.spend_native || 0) > 0
    && (r.closed_won_deals === null || r.closed_won_deals === undefined)).length;
  const legend = Object.keys(CAMP_STATUS_META).map((s) =>
    `<span class="dash-legend__item"><span class="dash-legend__swatch" style="background:${CAMP_STATUS_META[s].color}"></span>${escapeHtml(s)}</span>`).join("");
  return `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Campaign Performance Map</h3>
        <p class="dash-panel__sub">Spend (native GBP) × Closed-won deals · bubble size = SQLs · colour = status</p>
      </div>
    </div>
    ${campBubbleSVG(rows)}
    ${withheldN ? `<p class="dash-compare-note dash-compare-note--muted">${escapeHtml(fmtCount(withheldN))} campaign${withheldN === 1 ? "" : "s"} not plotted — closed-won deals withheld</p>` : ""}
    <div class="dash-legend camp-legend">${legend}</div>`;
}

// PR-ADS-161E: a campaign's customers are its distinct proven companies — not
// additive across campaigns — and withheld with the window's reason.
function campCustomersText(r) {
  const m = ((r || {}).metrics || {}).customers;
  if (m && m.status && m.status !== "published") {
    const why = canonicalReasonLabel(canonicalBlockingReason(m));
    return `${m.status === "withheld" ? "Withheld" : "Unavailable"}${why ? ` — ${why}` : ""}`;
  }
  return (r.customers === null || r.customers === undefined) ? "Unavailable" : String(r.customers);
}

function campBlockerOpts(d) {
  // PR-ADS-177A: `cohort` is the SAME response's acquisition-cohort block —
  // the argument every leads / SQLs cell gates on (cohortCountHtml).
  return { windowType: "business", windowKey: ((d || {}).window || {}).key,
           cohort: (d || {}).cohort };
}

// Legacy pages read the business window from the global selector.
function businessBlockerOpts() {
  return { windowType: "business", windowKey: getRoasBusinessWindow() };
}

function campBubbleSVG(rows) {
  if (!rows.length) {
    return '<div class="dash-chart-empty">No campaigns with verified spend in this window.</div>';
  }
  const W = 760, H = 320, padL = 60, padR = 24, padT = 20, padB = 44;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const maxX = dashNiceCeil(Math.max(1, ...rows.map((r) => r.spend_native || 0)) * 1.15);
  const maxY = dashNiceCeil(Math.max(1, ...rows.map((r) => r.closed_won_deals || 0)) * 1.15);
  const maxSql = Math.max(1, ...rows.map((r) => r.sqls || 0));
  const xOf = (v) => padL + plotW * ((v || 0) / maxX);
  const yOf = (v) => padT + plotH * (1 - (v || 0) / maxY);
  const rOf = (sqls) => (sqls === null || sqls === undefined) ? 6 : 8 + 22 * Math.sqrt((sqls || 0) / maxSql);

  const ticks = [0, 0.25, 0.5, 0.75, 1];
  const gridY = ticks.map((t) => {
    const y = (padT + plotH * (1 - t)).toFixed(1);
    return `<line x1="${padL}" x2="${W - padR}" y1="${y}" y2="${y}" stroke="#eef1f6" stroke-width="1"/>
      <text x="${padL - 8}" y="${Number(y) + 3}" text-anchor="end" class="dash-chart-tick">${escapeHtml(fmtCount(Math.round(maxY * t)))}</text>`;
  }).join("");
  const gridX = ticks.map((t) => {
    const x = xOf(maxX * t).toFixed(1);
    return `<text x="${x}" y="${H - 10}" text-anchor="middle" class="dash-chart-tick">${escapeHtml(fmtCompactCurrency(maxX * t, "GBP"))}</text>`;
  }).join("");

  // Carry the original index (avoid O(n²) indexOf inside the sorted map); large
  // bubbles drawn first so small ones stay hoverable on top.
  const bubbles = rows.map((r, i) => ({ r, i }))
    .sort((a, b) => rOf(b.r.sqls) - rOf(a.r.sqls)).map(({ r, i }) => {
      const rad = rOf(r.sqls);
      const cx = xOf(r.spend_native).toFixed(1), cy = yOf(r.closed_won_deals).toFixed(1);
      return `<circle class="dash-chart-hit camp-bubble" data-camp-bubble="${i}" cx="${cx}" cy="${cy}" r="${rad.toFixed(1)}"
      fill="${campStatusColor(r.status)}" fill-opacity="0.55" stroke="${campStatusColor(r.status)}" stroke-width="1.4"/>`;
    }).join("");

  return `
    <div class="dash-chart-wrap camp-bubble-wrap">
      <svg viewBox="0 0 ${W} ${H}" class="dash-combo-chart" role="img"
        aria-label="Campaign performance: native GBP spend on x, closed-won deals on y, SQLs as bubble size">
        ${gridY}
        <line x1="${padL}" x2="${W - padR}" y1="${(padT + plotH).toFixed(1)}" y2="${(padT + plotH).toFixed(1)}" stroke="#cbd5e1" stroke-width="1"/>
        <line x1="${padL}" x2="${padL}" y1="${padT}" y2="${(padT + plotH).toFixed(1)}" stroke="#cbd5e1" stroke-width="1"/>
        ${gridX}
        <text x="${(padL + plotW / 2).toFixed(1)}" y="${H - 26}" text-anchor="middle" class="dash-chart-axis">Spend (£) →</text>
        ${bubbles}
      </svg>
      <div class="dash-chart-tooltip" hidden></div>
    </div>`;
}

function wireCampBubbleHover(root, d) {
  const wrap = root.querySelector(".camp-bubble-wrap");
  if (!wrap) return;
  const tooltip = wrap.querySelector(".dash-chart-tooltip");
  const rows = campMapRows(d);

  function hide() {
    if (tooltip) tooltip.hidden = true;
    wrap.querySelectorAll(".camp-bubble.is-hover").forEach((el) => el.classList.remove("is-hover"));
  }
  wrap.addEventListener("pointermove", (e) => {
    const dot = e.target.closest(".camp-bubble");
    if (!dot || !tooltip) { hide(); return; }
    const r = rows[Number(dot.dataset.campBubble)];
    if (!r) { hide(); return; }
    wrap.querySelectorAll(".camp-bubble.is-hover").forEach((el) => el.classList.remove("is-hover"));
    dot.classList.add("is-hover");
    tooltip.textContent = "";
    const title = document.createElement("div");
    title.className = "dash-chart-tooltip__title";
    title.textContent = r.campaign_name || "";
    tooltip.appendChild(title);
    const usd = (r.spend_usd !== null && r.spend_usd !== undefined) ? fmtMoney(r.spend_usd) : "Unavailable";
    const ratio = (r.period_ratio_available && r.period_revenue_per_ad_spend !== null && r.period_revenue_per_ad_spend !== undefined) ? fmtPeriodRatio(r.period_revenue_per_ad_spend) : "Unavailable";
    [["Spend (£)", r.spend_native !== null && r.spend_native !== undefined ? fmtCompactCurrency(r.spend_native, r.native_currency || "GBP") : "Unavailable"],
     ["Spend (USD)", usd],
     ["SQLs", cohortCountText(r.sqls, d.cohort, r)],
     ["Won deals", r.closed_won_deals === null || r.closed_won_deals === undefined ? "Unavailable" : String(r.closed_won_deals)],
     ["Customers", campCustomersText(r)],
     ["Revenue", r.won_revenue_usd === null || r.won_revenue_usd === undefined ? "Unavailable" : fmtMoney(r.won_revenue_usd)],
     [PERIOD_RATIO_LABEL, ratio],
     ["Basis", PERIOD_RATIO_DISCLOSURE],
     ["Status", r.status || "—"]].forEach(([label, val]) => {
      const rowEl = document.createElement("div");
      rowEl.className = "dash-chart-tooltip__row";
      const strong = document.createElement("strong");
      strong.textContent = val;
      const name = document.createElement("span");
      name.textContent = ` ${label}`;
      rowEl.appendChild(strong);
      rowEl.appendChild(name);
      tooltip.appendChild(rowEl);
    });
    const wrapRect = wrap.getBoundingClientRect();
    const dotRect = dot.getBoundingClientRect();
    tooltip.hidden = false;
    const tipW = tooltip.offsetWidth || 170;
    let left = dotRect.left - wrapRect.left + dotRect.width / 2 - tipW / 2;
    left = Math.max(4, Math.min(left, wrapRect.width - tipW - 4));
    tooltip.style.left = `${left}px`;
    tooltip.style.top = "8px";
  });
  wrap.addEventListener("pointerleave", hide);
}

const CAMP_NEXT_ACTION = {
  "Revenue proven": "Scale — open Campaigns",
  "SQL producer": "Review SQL quality",
  "Spend without SQL / customer proof": "Review — spend without proof",
  "Revenue attribution missing": "Open Revenue Health",
  "FX unavailable": "FX incomplete — USD spend and period ratio withheld",
  "Mapping needs review": "Map campaign spend",
  "Closed-won outcome withheld": "Not judged — closed-won deals withheld",
  "SQLs withheld": "Not judged — SQLs withheld",
  // PR-ADS-177C (M5): the window's spend is not settled (before the morning
  // sync re-reads yesterday) — the row is not judged on spend still moving.
  "Spend not settled": "Not judged — spend not settled",
};

function renderCampLeaderboard(d) {
  const rows = d.campaigns || [];
  if (!rows.length) {
    return `
      <div class="dash-panel__header"><div><h3 class="dash-panel__title">Campaign Leaderboard</h3></div></div>
      <p class="rev-breakdown-empty">No campaign activity in this window.</p>`;
  }
  const head = ["Campaign", "Spend", COHORT_SQLS_LABEL + cohortThroughSuffix(d.cohort), "Won deals", "Customers", "Revenue", PERIOD_RATIO_LABEL + " / Status", "Next action"]
    .map((h) => `<th>${escapeHtml(h)}</th>`).join("");
  const body = rows.map((r, i) => {
    return `
    <tr class="camp-row" data-camp-row="${i}" tabindex="0" role="button" aria-label="Open ${escapeHtml(r.campaign_name || "campaign")} detail">
      <td class="camp-td camp-td--name">
        <span class="camp-dot ${(CAMP_STATUS_META[r.status] || {}).dot || "camp-dot--muted"}"></span>${escapeHtml(r.campaign_name || "—")}${campaignSharedNameHtml(r)}
      </td>
      <td class="camp-num">${campSpendCell(r)}</td>
      <td class="camp-num">${cohortCountHtml(r.sqls, d.cohort, r)}</td>
      <td class="camp-num">${rowMetricHtml(r, "closed_won_deals", fmtCount, campBlockerOpts(d))}</td>
      <td class="camp-num">${rowMetricHtml(r, "customers", fmtCount, campBlockerOpts(d))}</td>
      <td class="camp-num">${rowMetricHtml(r, "won_revenue_usd", fmtMoney, campBlockerOpts(d))}</td>
      <td>${campRoasCell(r)}${campaignSharedRatioNote(r)}</td>
      <td class="camp-next">${escapeHtml(campaignSharedCount(r)
        ? "Not judged — several campaigns share this name"
        : (CAMP_NEXT_ACTION[r.status] || "Open Campaigns"))}</td>
    </tr>`;
  }).join("") + renderCampLeaderboardResidual(d);
  return `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Campaign Leaderboard</h3>
        <p class="dash-panel__sub">Every campaign's spend, SQLs, closed-won deals, customers and revenue · click a row for proof · Period Revenue / Ad Spend only when spend + FX + revenue are safe · ${escapeHtml(PERIOD_RATIO_DISCLOSURE)}</p>
      </div>
    </div>
    <div class="camp-scroll">
      <table class="camp-table">
        <thead><tr>${head}</tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>`;
}

// PR-ADS-177B: the cohort's contacts on no campaign row, pinned last. Leads and
// SQLs only; every other cell is not campaign-attributable (never 0, never
// "—", never in a KPI). The keyword-theme panel (the legacy, non-windowed
// `keywords` snapshot) is dropped.
function renderCampLeaderboardResidual(d) {
  const res = d.campaign_residual_row;
  if (!res) return "";
  return `
    <tr class="camp-row--residual revenue-residual-row" aria-label="${escapeHtml(res.label || "No campaign label")} (not a campaign)">
      <td class="camp-td camp-td--name"><span class="camp-dot camp-dot--muted"></span>${escapeHtml(res.label || "No campaign label")} <span class="revenue-residual-badge">Residual</span></td>
      <td class="camp-num">${campaignNotAttributableHtml()}</td>
      <td class="camp-num">${cohortCountHtml(res.sqls, d.cohort, res)}</td>
      <td class="camp-num">${campaignNotAttributableHtml()}</td>
      <td class="camp-num">${campaignNotAttributableHtml()}</td>
      <td class="camp-num">${campaignNotAttributableHtml()}</td>
      <td>${campaignNotAttributableHtml()}</td>
      <td class="camp-next">Contacts with no campaign label</td>
    </tr>`;
}

function renderCampSignalList(title, terms, kind) {
  const body = !terms || !terms.length
    ? '<li class="rev-breakdown-empty">None in this window.</li>'
    : terms.map((t) => `
      <li class="camp-signal-row camp-signal-row--${kind}">
        <span class="camp-signal-row__term" title="${escapeHtml(t.search_term || "")}">${escapeHtml(t.search_term || "—")}</span>
        <span class="camp-signal-row__spend">${dashValue(t.spend_usd, fmtMoney)}</span>
        <span class="camp-signal-row__meta">${dashValue(t.clicks, fmtCount)} clicks${t.junk_category ? ` · ${escapeHtml(t.junk_category)}` : ""}</span>
      </li>`).join("");
  return `<div class="camp-signal-col camp-signal-col--${kind}">
    <div class="camp-signal-col__head">${escapeHtml(title)}</div>
    <ul class="camp-signal-list">${body}</ul>
  </div>`;
}

function renderCampSearchSignals(d) {
  const s = d.search_term_signals || {};
  const header = `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Search Term Signals</h3>
        <p class="dash-panel__sub">Waste-analysis evidence · <strong>read-only — no platform write</strong></p>
      </div>
    </div>`;
  if (s.status !== "ready") {
    return `${header}<p class="camp-keyword-note">Search-term signals are unavailable${s.reason ? ` — ${escapeHtml(s.reason)}` : ""}.</p>`;
  }
  const linkTerms = dashCanNavigate("search-terms")
    ? '<a class="camp-signal-link" href="#/search-terms">Review in Keywords &amp; Search Terms →</a>' : "";
  const linkWaste = dashCanNavigate("search-terms")
    ? '<a class="camp-signal-link" href="#/search-terms?tab=flagged">Open Flagged terms →</a>' : "";
  return `${header}
    <div class="camp-signal-grid">
      ${renderCampSignalList("Reviewed clean — not flagged waste", s.value_terms, "value")}
      ${renderCampSignalList("Waste — flagged", s.waste_terms, "waste")}
      ${renderCampSignalList("Needs review — unanalyzed", s.needs_review, "review")}
    </div>
    <div class="camp-signal-foot">${linkTerms}${linkWaste}<span class="camp-signal-note">Evidence only — no platform write.</span></div>`;
}

const CAMP_DEAL_COLUMNS = [
  ["deal_name", "Deal name"], ["company", "Company"], ["company_id", "Company ID"], ["main_contact", "Main Contact"],
  ["contact_id", "Contact ID"], ["deal", "Deal"], ["deal_id", "Deal ID"],
  ["amount_usd", "Amount"], ["close_date", "Close Date"], ["attribution_status", "Attribution"],
];

function campDealCell(deal, key) {
  if (key === "amount_usd") return dashValue(deal.amount_usd, fmtMoney); // null → Unavailable, never $0
  return dashValue(deal[key]);
}

function wireCampDrawers(root, d) {
  const rows = d.campaigns || [];
  const open = (i) => {
    const r = rows[i];
    if (!r) return;
    const deals = r.deals || [];
    const dealProofAvailable = d.deal_proof_available !== false;
    const dealHead = CAMP_DEAL_COLUMNS.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("");
    // When the deal ledger is unavailable we cannot prove OR disprove deals — say
    // Unavailable, never "No closed-won deals" (which would imply the campaign has none).
    // PR-ADS-161E: a withheld row carries no proof list (null) — say why, never
    // "no closed-won deals".
    const dealsWithheld = r.deals === null;
    const dealBody = (!dealProofAvailable || dealsWithheld)
      ? `<tr><td colspan="${CAMP_DEAL_COLUMNS.length}" class="dash-unavailable">${
          (((r.metrics || {}).closed_won_deals || {}).status === "withheld")
            ? "Closed-won deal proof withheld" : "Closed-won deal proof unavailable"} — ${
          escapeHtml(canonicalReasonLabel(canonicalBlockingReason((r.metrics || {}).closed_won_deals))
            || "the deal ledger could not be read")}${(() => {
          const href = crmBlockersHref({ ...campBlockerOpts(d),
            reason: canonicalBlockingReason((r.metrics || {}).closed_won_deals) });
          return href ? ` <a class="metric-blockers-link" href="${escapeHtml(href)}">View blockers</a>` : "";
        })()}</td></tr>`
      : deals.length
        ? deals.map((deal) => `<tr>${CAMP_DEAL_COLUMNS.map(([key]) => `<td class="camp-deal-td camp-deal-td--${key}">${campDealCell(deal, key)}</td>`).join("")}</tr>`).join("")
        : `<tr><td colspan="${CAMP_DEAL_COLUMNS.length}" class="rev-breakdown-empty">No closed-won deals to prove this campaign in this window.</td></tr>`;

    // PR-ADS-177C: one Keywords & Search Terms page.
    const links = [
      ["campaigns", "Open Campaigns"],
      ["search-terms", "Open Keywords & Search Terms"],
      ["revenue-health", "Open Revenue Health"],
    ].filter(([page]) => dashCanNavigate(page))
      .map(([page, label]) => `<a class="camp-drawer__link" href="#/${escapeHtml(page)}">${escapeHtml(label)} →</a>`).join("");

    let drawer = root.querySelector(".camp-drawer");
    if (!drawer) {
      drawer = document.createElement("div");
      drawer.className = "camp-drawer";
      root.querySelector(".camp-leaderboard-panel").appendChild(drawer);
    }
    drawer.innerHTML = `
      <div class="camp-drawer__head">
        <strong>${escapeHtml(r.campaign_name || "Campaign")}</strong>
        <span class="camp-status" style="--camp-accent:${campStatusColor(r.status)}">${escapeHtml(r.status || "")}</span>
        <button type="button" class="camp-drawer__close" aria-label="Close">×</button>
      </div>
      <dl class="camp-drawer__summary">
        <div><dt>Spend (native)</dt><dd>${campSpendCell(r)}</dd></div>
        <div><dt>${escapeHtml(COHORT_LEADS_LABEL + cohortThroughSuffix(d.cohort))}</dt><dd>${cohortCountHtml(r.leads, d.cohort, r)}</dd></div>
        <div><dt>${escapeHtml(COHORT_SQLS_LABEL + cohortThroughSuffix(d.cohort))}</dt><dd>${cohortCountHtml(r.sqls, d.cohort, r)}</dd></div>
        <div><dt>Won deals</dt><dd>${rowMetricHtml(r, "closed_won_deals", fmtCount, campBlockerOpts(d))}</dd></div>
        <div><dt>Customers</dt><dd>${rowMetricHtml(r, "customers", fmtCount, campBlockerOpts(d))}</dd></div>
        <div><dt>Revenue</dt><dd>${rowMetricHtml(r, "won_revenue_usd", fmtMoney, campBlockerOpts(d))}</dd></div>
        <div><dt>${PERIOD_RATIO_LABEL} / status</dt><dd>${campRoasCell(r)}${campaignSharedRatioNote(r)}<br><small class="metric-reason">${escapeHtml(PERIOD_RATIO_DISCLOSURE)}</small></dd></div>
        <div><dt>Mapping</dt><dd>${escapeHtml(r.attribution_status || "—")}</dd></div>
      </dl>
      ${campaignSharedIdsHtml(r)}
      <div class="camp-drawer__section">
        <h4 class="camp-drawer__h">Revenue proof — closed-won deals</h4>
        <div class="camp-deal-scroll">
          <table class="camp-deal-table"><thead><tr>${dealHead}</tr></thead><tbody>${dealBody}</tbody></table>
        </div>
      </div>
      <div class="camp-drawer__actions">${links}</div>`;
    drawer.querySelector(".camp-drawer__close").addEventListener("click", () => drawer.remove());
    drawer.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  root.querySelectorAll(".camp-row").forEach((tr) => {
    tr.addEventListener("click", () => open(Number(tr.dataset.campRow)));
    tr.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(Number(tr.dataset.campRow)); }
    });
  });
}

function renderCampTruthFooter(d) {
  const ts = d.truth_status || {};
  const chip = (label, status) => `
    <span class="dash-footer-chip" title="${escapeHtml(`${label}: ${status || "unknown"}`)}">
      <span class="dash-dot ${DASH_STATUS_DOT[status] || "dash-dot--muted"}"></span>${escapeHtml(label)}
    </span>`;
  const allGood = ts.spend === "ready" && ts.fx === "ready" && ts.revenue === "ready"
    && ts.campaign_attribution === "ready" && ts.deal_proof === "ready";
  return `
    <footer class="dash-truth-footer ${allGood ? "dash-truth-footer--ok" : ""}">
      ${chip("Google Ads spend", ts.spend)}
      ${chip("FX", ts.fx)}
      ${chip("Revenue", ts.revenue)}
      ${chip("Campaign attr.", ts.campaign_attribution)}
      ${chip("Deal proof", ts.deal_proof)}
      <span class="dash-footer-chip dash-footer-chip--readonly">Read-only</span>
      ${allGood
        ? '<span class="dash-footer-note">Campaign truth verified — search-term panels are read-only evidence.</span>'
        : (dashCanNavigate("revenue-health")
          ? '<a class="dash-footer-note" href="#/revenue-health">Some attribution needs review — open Revenue Health</a>'
          : '<span class="dash-footer-note">Some attribution needs review — contact your administrator.</span>')}
    </footer>`;
}

// ── Dashboard — Countries & Geo Intelligence tab (PR-ADS-138) ──────────────
//
// Fifth dashboard tab. Reuses the PR-134/135/136/137 design system end to end
// (KPI cards, chart shell, decision cards, truth footer, loading/refetch/
// unavailable states) — NO new visual language. It answers: which countries /
// markets produce SQLs, customers and closed-won revenue, and where is Google
// Ads spend present without business proof? Truth doctrine is identical: Google
// Ads spend is native GBP (never labelled USD); HubSpot closed-won is revenue
// truth (USD); geo ROAS only when a country has real attributed spend, FX +
// revenue are safe, and geo reconciliation is unblockable. The unattributed
// residual (spend with no country + revenue with no country) is PRESERVED in an
// explicit bucket — never mapped, never given a ROAS, never a drawer.

let dashCountries = null;
let dashCountriesStatus = "loading";

// Country status → colour + dot class (market map bubbles + badges).
const CTRY_STATUS_META = {
  "Revenue proven": { color: "#22b07d", dot: "ctry-dot--proven" },
  // PR-ADS-176A: computed from won DEALS, so the status no longer says "customer".
  "Won deal, revenue not proven": { color: "#3ac6a1", dot: "ctry-dot--customer" },
  "SQL producer": { color: "#129ef5", dot: "ctry-dot--sql" },
  "Spend without a won deal": { color: "#ff8a3d", dot: "ctry-dot--waste" },
  "FX unavailable": { color: "#8896a6", dot: "ctry-dot--muted" },
  "Geo attribution needs review": { color: "#7c5cff", dot: "ctry-dot--attr" },
  "No spend in window": { color: "#b6bfca", dot: "ctry-dot--muted" },
  "Closed-won outcome withheld": { color: "#8896a6", dot: "ctry-dot--muted" },
  // PR-ADS-177A: the acquisition cohort is withheld — not judged on SQLs.
  "SQLs withheld": { color: "#8896a6", dot: "ctry-dot--muted" },
};
function ctryStatusColor(status) {
  return (CTRY_STATUS_META[status] || { color: "#b6bfca" }).color;
}

const CTRY_NEXT_ACTION = {
  "Revenue proven": "Scale — open Countries",
  "Won deal, revenue not proven": "Confirm revenue attribution",
  "SQL producer": "Review SQL quality",
  "Spend without a won deal": "Review — spend without a won deal",
  "FX unavailable": "FX incomplete — USD spend and period ratio withheld",
  "Geo attribution needs review": "Open Revenue Health",
  "No spend in window": "No spend to judge",
  "Closed-won outcome withheld": "Not judged — closed-won deals withheld",
  "SQLs withheld": "Not judged — SQLs withheld",
};

async function loadDashboardCountries() {
  const window_ = getRoasBusinessWindow();
  const token = ++_revReqSeq.dashCountries;
  setWindowRangeLoading("dashboard-range");

  const root = document.getElementById("dashboard-countries-root");
  const genEl = document.getElementById("dashboard-generated");
  if (root) {
    if (dashCountries && dashCountriesStatus === "ok") {
      root.classList.add("is-refreshing");
    } else {
      root.innerHTML = renderDashSkeleton();
    }
  }

  try {
    const data = await fetchJSON(
      `/api/dashboard/countries?window=${encodeURIComponent(window_)}`
    );
    if (token !== _revReqSeq.dashCountries) return;
    if (!_revResponseIsCurrent("dashCountries", token, data)) return;
    dashCountries = data;
    dashCountriesStatus = "ok";
    renderWindowRange("dashboard-range", data.window || null);
    if (genEl) genEl.textContent = data.generated_at ? `Generated ${fmtDate(data.generated_at)}` : "";
    renderDashboardCountries();
  } catch (err) {
    console.error("[loadDashboardCountries]", err);
    if (token !== _revReqSeq.dashCountries) return;
    dashCountries = null;
    dashCountriesStatus = "error";
    if (genEl) genEl.textContent = "";
    renderWindowRange("dashboard-range", null);
    renderDashboardCountries();
  }
}

function renderDashboardCountries() {
  const root = document.getElementById("dashboard-countries-root");
  if (!root) return;
  root.classList.remove("is-refreshing");

  if (dashCountriesStatus !== "ok" || !dashCountries) {
    const nextStep = dashCanNavigate("health")
      ? 'Next step: check <a href="#/health">System Status</a> or retry with Refresh.'
      : "Next step: retry with Refresh, or contact your administrator.";
    root.innerHTML = `
      <div class="revenue-blocked-card dash-error-card">
        <h3 class="revenue-blocked-card__title">Countries &amp; Markets unavailable</h3>
        <p>The dashboard countries service could not be reached. Nothing is shown
        because nothing safe could be computed — metrics are never fabricated.</p>
        <p class="revenue-blocked-card__next">${nextStep}</p>
      </div>`;
    return;
  }

  const d = dashCountries;
  if (windowHasNoSettledDay(d.window)) {
    // PR-ADS-167A: nothing measured yet — the disclosure, not the cards.
    root.innerHTML = renderNoSettledDayCard(d.window);
    return;
  }
  root.innerHTML = `
    <div class="dash-tab-intro dash-anim">
      <h2 class="dash-tab-intro__title">Countries &amp; Markets</h2>
      <p class="dash-tab-intro__sub">Where Google Ads spend, SQLs, customers and closed-won revenue are actually showing up. Spend is native GBP; revenue is HubSpot closed-won USD.</p>
    </div>
    ${ownerLabelMismatchNotice(d.owner_labelled_geo)}
    ${renderCtryKpiRow(d)}
    <section class="dash-panel dash-chart-panel dash-anim ctry-map-panel" aria-label="Geo performance map">
      ${renderCtryGeoMap(d)}
    </section>
    <section class="dash-panel dash-anim ctry-leaderboard-panel" aria-label="Country leaderboard">
      ${renderCtryLeaderboard(d)}
    </section>
    <div class="dash-main-grid ctry-lower-grid">
      <section class="dash-panel dash-anim ctry-region-panel" aria-label="Regional mix">
        ${renderCtryRegionalMix(d)}
      </section>
      <aside class="dash-panel dash-anim ctry-residual-panel" aria-label="Unattributed residual">
        ${renderCtryResidualPanel(d)}
      </aside>
    </div>
    <section class="dash-decisions dash-anim" aria-label="Geo signal cards">
      ${renderDashDecisionCards(d)}
    </section>
    ${renderCtryTruthFooter(d)}
  `;
  wireCtryBubbleHover(root, d);
  wireCtryDrawers(root, d);
}

// Native GBP spend cell (never a "$"), with USD appended only when FX-safe.
function ctrySpendCell(row) {
  if (row.spend_native === null || row.spend_native === undefined) {
    // PR-ADS-176A truth audit MINOR 6: Unavailable WITH the reason the row carries.
    return countrySpendUnavailableHtml(row.spend_reason, "dash-unavailable");
  }
  // PR-ADS-176B-F2: a published $0 resting on owner labels says so.
  const zeroNote = row.spend_native === 0 ? ownerZeroNote(row) : "";
  const native = escapeHtml(fmtCompactCurrency(row.spend_native, row.native_currency || "GBP"));
  if (row.spend_usd !== null && row.spend_usd !== undefined) {
    return `${native} <span class="ctry-usd">· ${escapeHtml(fmtMoney(row.spend_usd))}</span>${zeroNote}`;
  }
  return native + zeroNote;
}

function ctryRoasCell(row) {
  if (row.period_ratio_available && row.period_revenue_per_ad_spend !== null
      && row.period_revenue_per_ad_spend !== undefined) {
    return `<span class="ctry-roas" title="${escapeHtml(PERIOD_RATIO_DISCLOSURE)}">${escapeHtml(fmtPeriodRatio(row.period_revenue_per_ad_spend))}</span>`;
  }
  return `<span class="ctry-status" style="--ctry-accent:${ctryStatusColor(row.status)}">${escapeHtml(row.status || "—")}</span>`;
}

// Compact native-currency magnitude for the hero value (symbol + magnitude, no
// trailing code word) so a long "£10.0k GBP" never truncates. The £ symbol makes
// the currency unambiguous; the currency code is restated in the sub line.
function ctryNativeBig(native) {
  if (!native || native.amount === null || native.amount === undefined) {
    return '<span class="dash-unavailable">Unavailable</span>';
  }
  const sym = currencySymbol(native.currency || "GBP");
  const abs = Math.abs(native.amount);
  let body;
  if (abs >= 1e6) body = (native.amount / 1e6).toFixed(1) + "M";
  else if (abs >= 1e3) body = (native.amount / 1e3).toFixed(1) + "k";
  else body = Number(native.amount).toFixed(0);
  return escapeHtml(`${sym}${body}`);
}

// PR-ADS-167A: why the country spend card is empty, from the reason code the
// page's own metric contract carries (e.g. before the morning sync re-reads
// yesterday). Open windows end yesterday, so after that sync it is not shown.
function ctrySpendWithheldText(d) {
  const code = (((d && d.metric_truth) || {}).country_attributed_spend_usd || {}).unavailable_reason;
  const why = code ? GEO_COVERAGE_REASONS[code] : null;
  return why ? `Country spend withheld — ${why}` : "Country spend withheld";
}

/** PR-ADS-170A-F1: the spend card is the listed markets' spend. When Google
 *  Ads geo spend lies outside them (a country that is not a listed market) the
 *  card says how much; that spend is on no row of this page. "" when none,
 *  or when the server did not compute it. */
function ctryOutsideMarketsText(k) {
  const o = (k || {}).spend_outside_listed_markets;
  if (!o || !(o.native > 0)) return "";
  const names = Array.isArray(o.countries) && o.countries.length ? ` (${o.countries.join(", ")})` : "";
  return ` · not included: ${escapeHtml(fmtCompactCurrency(o.native, o.currency || "GBP"))} in countries outside the listed markets${escapeHtml(names)}`;
}

function renderCtryKpiRow(d) {
  const k = d.kpis || {};
  const pc = d.period_change || {};
  const native = k.verified_spend_native || {};
  const nativeVal = ctryNativeBig(native);
  const cur = escapeHtml(native.currency || "GBP");
  // PR-ADS-167A: with no native figure either, the reason is not FX.
  const usdSub = (k.verified_spend_usd !== null && k.verified_spend_usd !== undefined)
    ? `${cur} native · ${escapeHtml(fmtMoney(k.verified_spend_usd))} USD`
    : (native.amount === null || native.amount === undefined)
      ? escapeHtml(ctrySpendWithheldText(d))
      : `${cur} native · USD unavailable (FX)`;

  const cards = [
    {
      key: "spend", label: "Verified Geo Spend",
      value: nativeVal, sub: usdSub + ctryOutsideMarketsText(k),
      delta: dashDeltaChip("verified_spend_usd", pc),
      source: "Google Ads · native GBP · country-attributed · listed markets only",
      ok: native.amount !== null && native.amount !== undefined,
    },
    {
      // PR-ADS-177A: the acquisition cohort, gated on THIS response's cohort;
      // under a published cohort the total may still be withheld by the gate.
      key: "sqls", label: COHORT_SQLS_LABEL,
      value: cohortCountHtml(k.sqls, d.cohort, { sqls_status: k.sqls_status, sqls_reason: k.sqls_reason }),
      sub: (k.sqls_residual === null || k.sqls_residual === undefined)
        ? `Google Ads contacts · by the contact's country · listed markets${cohortThroughSuffix(d.cohort)}`
        : `Google Ads contacts · listed markets · ${cohortCountHtml(k.sqls_residual, d.cohort, null)} in the residual${cohortThroughSuffix(d.cohort)}`,
      delta: cohortDeltaChip("sqls", pc, d),
      source: "HubSpot acquisition cohort",
      ok: cohortCountPublication(d.cohort).publish && k.sqls !== null && k.sqls !== undefined,
    },
    {
      // PR-ADS-161E: closed-won DEALS. Country rows partition deals; their
      // customers (companies) are not additive, so no customer total is summed.
      key: "deals", label: "Closed-Won Deals",
      value: dashValue(k.closed_won_deals, fmtCount),
      sub: `Google Ads-source · ${(k.countries_with_closed_won_deals === null || k.countries_with_closed_won_deals === undefined) ? "—" : fmtCount(k.countries_with_closed_won_deals)} countries`,
      delta: dashDeltaChip("closed_won_deals", pc),
      source: "HubSpot Closed-Won",
      ok: k.closed_won_deals !== null && k.closed_won_deals !== undefined,
    },
    {
      key: "revenue", label: "Won Revenue",
      value: canonicalValueHtml(k.won_revenue_usd, k.revenue_metric, fmtMoney, { windowType: "business", windowKey: (d.window || {}).key }), sub: "USD · HubSpot closed-won",
      delta: dashDeltaChip("won_revenue_usd", pc),
      source: "HubSpot Closed-Won",
      ok: k.won_revenue_usd !== null && k.won_revenue_usd !== undefined,
    },
    {
      key: "geo_period_revenue_per_ad_spend", label: "Geo " + PERIOD_RATIO_LABEL,
      value: (k.geo_period_revenue_per_ad_spend !== null && k.geo_period_revenue_per_ad_spend !== undefined)
        ? escapeHtml(fmtPeriodRatio(k.geo_period_revenue_per_ad_spend))
        : '<span class="dash-unavailable">Unavailable</span>',
      sub: `${(k.countries_with_spend === null || k.countries_with_spend === undefined) ? "Countries with spend withheld" : `${fmtCount(k.countries_with_spend)} countries with spend`} · ${PERIOD_RATIO_DISCLOSURE}`,
      delta: dashDeltaChip("geo_period_revenue_per_ad_spend", pc),
      source: "Revenue closed this window ÷ this window's verified USD spend",
      ok: k.geo_period_revenue_per_ad_spend !== null && k.geo_period_revenue_per_ad_spend !== undefined,
    },
    {
      key: "residual", label: "Residual Revenue",
      value: dashValue(k.residual_revenue_usd, fmtMoney),
      sub: `${(k.residual_closed_won_deals === null || k.residual_closed_won_deals === undefined) ? "Unavailable" : fmtCount(k.residual_closed_won_deals)} won deals · no country`,
      delta: '<span class="dash-delta dash-delta--none">Isolated</span>',
      source: "Preserved · never distributed",
      ok: k.residual_revenue_usd !== null && k.residual_revenue_usd !== undefined,
    },
  ];

  const compareNote = pc.available
    ? `<p class="dash-compare-note">Change ${escapeHtml(pc.label || "vs previous period")}${pc.note ? ` — ${escapeHtml(pc.note)}` : ""}</p>`
    : `<p class="dash-compare-note dash-compare-note--muted">No previous-period comparison${pc.reason ? ` — ${escapeHtml(pc.reason)}` : ""}</p>`;

  return `
    <div class="dash-kpi-grid ctry-kpi-grid">
      ${cards.map((c) => `
        <div class="dash-kpi-card dash-anim ${c.ok ? "" : "dash-kpi-card--unavailable"}" data-kpi="${c.key}">
          <div class="dash-kpi-card__label">${escapeHtml(c.label)}</div>
          <div class="dash-kpi-card__value">${c.value}</div>
          <div class="dash-kpi-card__sub">${escapeHtml(c.sub)}</div>
          <div class="dash-kpi-card__meta">${c.delta}</div>
          <div class="dash-kpi-card__source">${escapeHtml(c.source)}</div>
        </div>`).join("")}
    </div>
    ${compareNote}`;
}

// Market map: region lanes, each a horizontal strip of country bubbles. Bubble
// size = closed-won deals (falls back to native spend when no deals anywhere so the
// map is never uniformly flat); colour = business status. The residual is NEVER
// mapped — it is not a country.
function ctryMapSizeMetric(rows) {
  // Prefer won deals; if no country has any — or any market's count is
  // withheld (PR-ADS-161E: never sized as if it were 0) — use native spend.
  const anyWithheld = rows.some((r) => r.closed_won_deals === null || r.closed_won_deals === undefined);
  const anyDeals = !anyWithheld && rows.some((r) => (r.closed_won_deals || 0) > 0);
  const metric = anyDeals
    ? (r) => r.closed_won_deals || 0
    : (r) => r.spend_native || 0;
  return { metric, label: anyDeals ? "closed-won deals" : "native spend" };
}

function renderCtryGeoMap(d) {
  const rows = (d.countries || []).filter((r) => !r.is_residual);
  const legend = Object.keys(CTRY_STATUS_META).map((s) =>
    `<span class="dash-legend__item"><span class="dash-legend__swatch" style="background:${CTRY_STATUS_META[s].color}"></span>${escapeHtml(s)}</span>`).join("");
  const header = `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Geo Performance Map</h3>
        <p class="dash-panel__sub">Countries grouped by region · bubble size = closed-won deals · colour = status · click a market for proof</p>
      </div>
    </div>`;
  if (!rows.length) {
    return `${header}<div class="dash-chart-empty">No country activity in this window.</div>`;
  }

  const { metric, label } = ctryMapSizeMetric(rows);
  const maxMetric = Math.max(1, ...rows.map(metric));
  const rOf = (r) => 12 + 26 * Math.sqrt(metric(r) / maxMetric);

  // Preserve the original index (bubbles reference d.countries by index for
  // hover + drawer). Build the lookup once (O(n)) rather than an indexOf scan per
  // row (O(n²)), matching the other dashboard maps.
  const countryIndex = new Map((d.countries || []).map((c, i) => [c, i]));
  const indexed = rows.map((r) => ({ r, idx: countryIndex.get(r) }));
  const byRegion = {};
  indexed.forEach((entry) => {
    const region = entry.r.region || "Unknown / Needs Review";
    (byRegion[region] = byRegion[region] || []).push(entry);
  });
  const REGION_ORDER = ["North America", "Europe", "Middle East", "LATAM", "Africa", "APAC", "Unknown / Needs Review"];
  const regions = REGION_ORDER.filter((rg) => byRegion[rg]);

  const lanes = regions.map((region) => {
    const entries = byRegion[region].slice().sort((a, b) => metric(b.r) - metric(a.r));
    const pad = 10, gap = 14, laneH = 118, labelH = 22;
    let x = pad;
    const nodes = entries.map(({ r, idx }) => {
      const rad = rOf(r);
      const cx = x + rad;
      x = cx + rad + gap;
      const cy = (laneH - labelH) / 2;
      const code = r.country_code || (r.country_name || "").slice(0, 3).toUpperCase();
      return `
        <g class="ctry-bubble-g">
          <circle class="dash-chart-hit ctry-bubble" data-ctry-bubble="${idx}" cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="${rad.toFixed(1)}"
            fill="${ctryStatusColor(r.status)}" fill-opacity="0.55" stroke="${ctryStatusColor(r.status)}" stroke-width="1.4"/>
          <text x="${cx.toFixed(1)}" y="${(laneH - labelH + 14).toFixed(1)}" text-anchor="middle" class="ctry-bubble-label">${escapeHtml(code)}</text>
        </g>`;
    }).join("");
    const svgW = Math.max(x + pad, 200);
    // Region revenue: Unavailable (never a fabricated $0) if any market's revenue
    // is unknown — e.g. a disconnected revenue integration blanks them all.
    const wonVals = byRegion[region].map((e) => e.r.won_revenue_usd);
    const won = wonVals.some((v) => v === null || v === undefined)
      ? null : wonVals.reduce((acc, v) => acc + v, 0);
    return `
      <div class="ctry-lane">
        <div class="ctry-lane__head">
          <span class="ctry-lane__region">${escapeHtml(region)}</span>
          <span class="ctry-lane__meta">${escapeHtml(fmtCount(entries.length))} ${entries.length === 1 ? "market" : "markets"} · ${escapeHtml(_fmtUsdShort(won))}</span>
        </div>
        <div class="ctry-lane__strip">
          <svg viewBox="0 0 ${svgW} 118" width="${svgW}" height="118" class="ctry-lane-svg" role="img" aria-label="${escapeHtml(region)} markets">
            ${nodes}
          </svg>
        </div>
      </div>`;
  }).join("");

  return `${header}
    <div class="ctry-map">${lanes}</div>
    <div class="dash-legend ctry-legend">${legend}<span class="ctry-legend__note">Bubble size = ${escapeHtml(label)}</span></div>
    <div class="dash-chart-tooltip ctry-map-tooltip" hidden></div>`;
}

function _fmtUsdShort(v) {
  if (v === null || v === undefined) return "Unavailable";
  const n = Number(v);
  if (!isFinite(n)) return "Unavailable";
  if (Math.abs(n) >= 1e6) return "$" + (n / 1e6).toFixed(1) + "M";
  if (Math.abs(n) >= 1e3) return "$" + (n / 1e3).toFixed(1) + "k";
  return "$" + n.toFixed(0);
}

function wireCtryBubbleHover(root, d) {
  const map = root.querySelector(".ctry-map-panel");
  if (!map) return;
  const tooltip = map.querySelector(".ctry-map-tooltip");
  const rows = d.countries || [];

  function hide() {
    if (tooltip) tooltip.hidden = true;
    map.querySelectorAll(".ctry-bubble.is-hover").forEach((el) => el.classList.remove("is-hover"));
  }
  map.addEventListener("pointermove", (e) => {
    const dot = e.target.closest(".ctry-bubble");
    if (!dot || !tooltip) { hide(); return; }
    const r = rows[Number(dot.dataset.ctryBubble)];
    if (!r) { hide(); return; }
    map.querySelectorAll(".ctry-bubble.is-hover").forEach((el) => el.classList.remove("is-hover"));
    dot.classList.add("is-hover");
    tooltip.textContent = "";
    const title = document.createElement("div");
    title.className = "dash-chart-tooltip__title";
    title.textContent = `${r.country_name || "—"}${ownerLabelledCountryText(r.spend_country_provenance)} · ${r.region || ""}`;
    tooltip.appendChild(title);
    const usd = (r.spend_usd !== null && r.spend_usd !== undefined) ? fmtMoney(r.spend_usd) : "Unavailable";
    const ratio = (r.period_ratio_available && r.period_revenue_per_ad_spend !== null && r.period_revenue_per_ad_spend !== undefined) ? fmtPeriodRatio(r.period_revenue_per_ad_spend) : "Unavailable";
    [["Spend (£)", r.spend_native !== null && r.spend_native !== undefined ? fmtCompactCurrency(r.spend_native, r.native_currency || "GBP") : "Unavailable"],
     ["Spend (USD)", usd],
     ["SQLs", cohortCountText(r.sqls, d.cohort, r)],
     ["Won deals", r.closed_won_deals === null || r.closed_won_deals === undefined ? "Unavailable" : String(r.closed_won_deals)],
     ["Customers", campCustomersText(r)],
     ["Revenue", r.won_revenue_usd === null || r.won_revenue_usd === undefined ? "Unavailable" : fmtMoney(r.won_revenue_usd)],
     [PERIOD_RATIO_LABEL, ratio],
     ["Basis", PERIOD_RATIO_DISCLOSURE],
     ["Status", r.status || "—"]].forEach(([lbl, val]) => {
      const rowEl = document.createElement("div");
      rowEl.className = "dash-chart-tooltip__row";
      const strong = document.createElement("strong");
      strong.textContent = val;
      const name = document.createElement("span");
      name.textContent = ` ${lbl}`;
      rowEl.appendChild(strong);
      rowEl.appendChild(name);
      tooltip.appendChild(rowEl);
    });
    const mapRect = map.getBoundingClientRect();
    const dotRect = dot.getBoundingClientRect();
    tooltip.hidden = false;
    const tipW = tooltip.offsetWidth || 180;
    let left = dotRect.left - mapRect.left + dotRect.width / 2 - tipW / 2;
    left = Math.max(4, Math.min(left, mapRect.width - tipW - 4));
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${Math.max(4, dotRect.top - mapRect.top - 8)}px`;
  });
  map.addEventListener("pointerleave", hide);
}

function renderCtryLeaderboard(d) {
  const real = (d.countries || []).filter((r) => !r.is_residual);
  const residual = d.residual || null;
  if (!real.length && !residual) {
    return `
      <div class="dash-panel__header"><div><h3 class="dash-panel__title">Country Leaderboard</h3></div></div>
      <p class="rev-breakdown-empty">No country activity in this window.</p>`;
  }
  const head = ["Country", "Region", "Spend", COHORT_SQLS_LABEL + cohortThroughSuffix(d.cohort), "Won deals", "Customers", "Revenue", PERIOD_RATIO_LABEL + " / Status", "Next action"]
    .map((h) => `<th>${escapeHtml(h)}</th>`).join("");
  const body = real.map((r, i) => `
    <tr class="ctry-row" data-ctry-row="${i}" tabindex="0" role="button" aria-label="Open ${escapeHtml(r.country_name || "country")} detail">
      <td class="ctry-td ctry-td--name">
        <span class="ctry-dot ${(CTRY_STATUS_META[r.status] || {}).dot || "ctry-dot--muted"}"></span>${escapeHtml(r.country_name || "—")}${spendOwnerNote(r)}
      </td>
      <td class="ctry-td--region">${escapeHtml(r.region || "—")}</td>
      <td class="ctry-num">${ctrySpendCell(r)}</td>
      <td class="ctry-num">${cohortCountHtml(r.sqls, d.cohort, r)}</td>
      <td class="ctry-num">${rowMetricHtml(r, "closed_won_deals", fmtCount, campBlockerOpts(d))}</td>
      <td class="ctry-num">${rowMetricHtml(r, "customers", fmtCount, campBlockerOpts(d))}</td>
      <td class="ctry-num">${rowMetricHtml(r, "won_revenue_usd", fmtMoney, campBlockerOpts(d))}</td>
      <td>${ctryRoasCell(r)}</td>
      <td class="ctry-next">${escapeHtml(CTRY_NEXT_ACTION[r.status] || "Open Countries")}</td>
    </tr>`).join("");

  // Residual row — pinned to the bottom, NOT clickable, never a ROAS. It is the
  // isolated "no country" bucket, never treated as a real market.
  let residualRow = "";
  if (residual) {
    residualRow = `
      <tr class="ctry-row ctry-row--residual" aria-label="Unattributed residual (not a country)">
        <td class="ctry-td ctry-td--name"><span class="ctry-dot ctry-dot--muted"></span>${escapeHtml(residual.label || "Unknown / Unattributed country")}</td>
        <td class="ctry-td--region">—</td>
        <td class="ctry-num">${ctrySpendCell(residual)}</td>
        <td class="ctry-num"><span class="dash-unavailable">Unavailable</span></td>
        <td class="ctry-num">${dashValue(residual.closed_won_deals, fmtCount)}</td>
        <td class="ctry-num"><span class="dash-unavailable" title="Customers are distinct companies; a residual of companies cannot be derived by subtraction">Unavailable</span></td>
        <td class="ctry-num">${dashValue(residual.won_revenue_usd, fmtMoney)}</td>
        <td><span class="ctry-status ctry-status--residual">Unattributed</span></td>
        <td class="ctry-next">Preserved — never distributed</td>
      </tr>`;
  }
  return `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Country Leaderboard</h3>
        <p class="dash-panel__sub">Every market's spend, SQLs, closed-won deals, customers and revenue · click a row for proof · Period Revenue / Ad Spend only when spend + FX + revenue + geo reconciliation are safe · ${escapeHtml(PERIOD_RATIO_DISCLOSURE)}</p>
      </div>
    </div>
    <div class="ctry-scroll">
      <table class="ctry-table">
        <thead><tr>${head}</tr></thead>
        <tbody>${body}${residualRow}</tbody>
      </table>
    </div>`;
}

// Regional mix — horizontal bars. Metric chosen by availability (revenue →
// won deals → native spend); never a fabricated percentage when the denominator
// is missing (a region with an unavailable metric shows no bar).
function _ctryRegionalBarMetric(rows) {
  if (rows.some((r) => (r.won_revenue_usd || 0) > 0)) return "won_revenue_usd";
  if (rows.some((r) => (r.closed_won_deals || 0) > 0)) return "closed_won_deals";
  if (rows.some((r) => (r.spend_native || 0) > 0)) return "spend_native";
  return null;
}

function renderCtryRegionalMix(d) {
  const rows = d.regional_mix || [];
  const header = `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Regional Mix</h3>
        <p class="dash-panel__sub">Where spend, closed-won deals and revenue concentrate</p>
      </div>
    </div>`;
  if (!rows.length) {
    return `${header}<p class="rev-breakdown-empty">No regional activity in this window.</p>`;
  }
  const metricKey = _ctryRegionalBarMetric(rows);
  const metricLabel = { won_revenue_usd: "revenue", closed_won_deals: "closed-won deals", spend_native: "native spend" }[metricKey] || "activity";
  const max = metricKey ? Math.max(1, ...rows.map((r) => (r[metricKey] === null || r[metricKey] === undefined) ? 0 : r[metricKey])) : 0;
  const bars = rows.map((r) => {
    const val = metricKey ? r[metricKey] : null;
    const pct = (metricKey && val !== null && val !== undefined && max > 0) ? Math.max(2, (val / max) * 100) : 0;
    const barVal = (val === null || val === undefined)
      ? '<span class="dash-unavailable">Unavailable</span>'
      : (metricKey === "won_revenue_usd" ? escapeHtml(fmtMoney(val))
         : metricKey === "spend_native" ? escapeHtml(fmtCompactCurrency(val, r.native_currency || "GBP"))
         : escapeHtml(fmtCount(val)));
    return `
      <li class="ctry-region-row">
        <div class="ctry-region-row__top">
          <span class="ctry-region-row__name"><span class="ctry-dot ${(CTRY_STATUS_META[r.status] || {}).dot || "ctry-dot--muted"}"></span>${escapeHtml(r.region || "—")}</span>
          <span class="ctry-region-row__val">${barVal}</span>
        </div>
        <div class="ctry-region-bar"><span class="ctry-region-bar__fill" style="width:${pct.toFixed(1)}%;background:${ctryStatusColor(r.status)}"></span></div>
        <div class="ctry-region-row__meta">
          <span>${escapeHtml(fmtCount(r.countries || 0))} ${r.countries === 1 ? "market" : "markets"}</span>
          <span>Spend ${r.spend_native === null || r.spend_native === undefined ? "—" : escapeHtml(fmtCompactCurrency(r.spend_native, r.native_currency || "GBP"))}</span>
          <span>Won deals ${dashValue(r.closed_won_deals, fmtCount)}</span>
          <span title="${escapeHtml(PERIOD_RATIO_DISCLOSURE)}">${r.period_ratio_available && r.period_revenue_per_ad_spend !== null && r.period_revenue_per_ad_spend !== undefined ? escapeHtml(PERIOD_RATIO_LABEL + " " + fmtPeriodRatio(r.period_revenue_per_ad_spend) + " · " + PERIOD_RATIO_DISCLOSURE) : escapeHtml(r.status || "—")}</span>
        </div>
      </li>`;
  }).join("");
  return `${header}
    <p class="ctry-region-note">Bars scaled by ${escapeHtml(metricLabel)}${metricKey ? "" : " — unavailable"}.</p>
    <ul class="ctry-region-list">${bars}</ul>`;
}

function renderCtryResidualPanel(d) {
  const res = d.residual || null;
  const header = `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Unknown / Unattributed country</h3>
        <p class="dash-panel__sub">Isolated — never distributed across markets</p>
      </div>
    </div>`;
  if (!res) {
    return `${header}<p class="rev-breakdown-empty">No unattributed residual in this window.</p>`;
  }
  return `${header}
    <dl class="ctry-residual-tiles">
      <div><dt>Residual revenue</dt><dd>${dashValue(res.won_revenue_usd, fmtMoney)}</dd></div>
      <div><dt>Closed-won deals</dt><dd>${dashValue(res.closed_won_deals, fmtCount)}</dd></div>
      <div><dt>${escapeHtml(COHORT_SQLS_LABEL + cohortThroughSuffix(d.cohort))}</dt><dd>${cohortCountHtml(res.sqls, d.cohort, res)}</dd></div>
      <div><dt>Geo spend residual</dt><dd>${res.spend_native === null || res.spend_native === undefined ? '<span class="dash-unavailable">Unavailable</span>' : escapeHtml(fmtCompactCurrency(res.spend_native, res.native_currency || "GBP"))}</dd></div>
    </dl>
    <p class="ctry-residual-note">${escapeHtml(res.reason || "Revenue and spend with no country are preserved separately — never spread across real countries.")}</p>`;
}

const CTRY_DEAL_COLUMNS = [
  ["deal_name", "Deal name"], ["company", "Company"], ["company_id", "Company ID"], ["main_contact", "Main Contact"],
  ["contact_id", "Contact ID"], ["deal", "Deal"], ["deal_id", "Deal ID"],
  ["amount_usd", "Amount"], ["close_date", "Close Date"], ["campaign_name", "Campaign (source)"],
  ["attribution_status", "Attribution"],
];

function ctryDealCell(deal, key) {
  if (key === "amount_usd") return dashValue(deal.amount_usd, fmtMoney); // null → Unavailable, never $0
  return dashValue(deal[key]);
}

function wireCtryDrawers(root, d) {
  const rows = (d.countries || []).filter((r) => !r.is_residual);
  const open = (i) => {
    const r = rows[i];
    if (!r) return;
    const dealHead = CTRY_DEAL_COLUMNS.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("");
    // deal_proof_available === false → "unavailable"; deals === null → withheld.
    const dealBody = ctryDealBody(r, d);

    const links = [
      ["roas-countries", "Open Countries"],
      ["pipeline", "Open Pipeline"],
      ["revenue-health", "Open Revenue Health"],
    ].filter(([page]) => dashCanNavigate(page))
      .map(([page, label]) => `<a class="ctry-drawer__link" href="#/${escapeHtml(page)}">${escapeHtml(label)} →</a>`).join("");

    let drawer = root.querySelector(".ctry-drawer");
    if (!drawer) {
      drawer = document.createElement("div");
      drawer.className = "ctry-drawer";
      root.querySelector(".ctry-leaderboard-panel").appendChild(drawer);
    }
    drawer.innerHTML = `
      <div class="ctry-drawer__head">
        <strong>${escapeHtml(r.country_name || "Country")}</strong>${spendOwnerNote(r)}
        <span class="ctry-drawer__region">${escapeHtml(r.region || "")}</span>
        <span class="ctry-status" style="--ctry-accent:${ctryStatusColor(r.status)}">${escapeHtml(r.status || "")}</span>
        <button type="button" class="ctry-drawer__close" aria-label="Close">×</button>
      </div>
      <dl class="ctry-drawer__summary">
        <div><dt>Spend (native)</dt><dd>${ctrySpendCell(r)}</dd></div>
        <div><dt>${escapeHtml(COHORT_LEADS_LABEL + cohortThroughSuffix(d.cohort))}</dt><dd>${cohortCountHtml(r.leads, d.cohort, r)}</dd></div>
        <div><dt>${escapeHtml(COHORT_SQLS_LABEL + cohortThroughSuffix(d.cohort))}</dt><dd>${cohortCountHtml(r.sqls, d.cohort, r)}</dd></div>
        <div><dt>Won deals</dt><dd>${rowMetricHtml(r, "closed_won_deals", fmtCount, campBlockerOpts(d))}</dd></div>
        <div><dt>Customers</dt><dd>${rowMetricHtml(r, "customers", fmtCount, campBlockerOpts(d))}</dd></div>
        <div><dt>Revenue</dt><dd>${rowMetricHtml(r, "won_revenue_usd", fmtMoney, campBlockerOpts(d))}</dd></div>
        <div><dt>Geo ${PERIOD_RATIO_LABEL} / status</dt><dd>${ctryRoasCell(r)}<br><small class="metric-reason">${escapeHtml(PERIOD_RATIO_DISCLOSURE)}</small></dd></div>
        <div><dt>Top campaign</dt><dd>${dashValue(r.top_campaign)}</dd></div>
      </dl>
      <div class="ctry-drawer__section">
        <h4 class="ctry-drawer__h">Revenue proof — closed-won deals</h4>
        <div class="ctry-deal-scroll">
          <table class="ctry-deal-table"><thead><tr>${dealHead}</tr></thead><tbody>${dealBody}</tbody></table>
        </div>
      </div>
      <div class="ctry-drawer__actions">${links}</div>`;
    drawer.querySelector(".ctry-drawer__close").addEventListener("click", () => drawer.remove());
    drawer.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  root.querySelectorAll(".ctry-row:not(.ctry-row--residual)").forEach((tr) => {
    tr.addEventListener("click", () => open(Number(tr.dataset.ctryRow)));
    tr.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(Number(tr.dataset.ctryRow)); }
    });
  });
  // Map bubbles open the same drawer (bubbles carry the d.countries index).
  root.querySelectorAll(".ctry-bubble").forEach((b) => {
    b.addEventListener("click", () => {
      const all = d.countries || [];
      const r = all[Number(b.dataset.ctryBubble)];
      if (!r || r.is_residual) return;
      const idx = rows.indexOf(r);
      if (idx >= 0) open(idx);
    });
  });
}

function ctryDealBody(r, d) {
  const n = CTRY_DEAL_COLUMNS.length;
  const none = `<tr><td colspan="${n}" class="rev-breakdown-empty">No closed-won deals to prove this country in this window.</td></tr>`;
  const noProof = `<tr><td colspan="${n}" class="dash-unavailable">Closed-won deal proof unavailable — the deal ledger could not be read (revenue may still be canonical).</td></tr>`;
  // The drawer's deal rows. When the deal ledger is unavailable we cannot prove
  // OR disprove deals — say Unavailable, never "No closed-won deals" (which would
  // imply the country has none). PR-ADS-176A round 2: `deals: null` is a
  // withheld population (the row's deal count is withheld) — said as withheld.
  if (d && d.deal_proof_available === false) return noProof;
  if (!r || r.deals === null || r.deals === undefined) {
    return `<tr><td colspan="${n}" class="dash-unavailable">Closed-won deals are withheld for this country in this window.</td></tr>`;
  }
  return r.deals.length
    ? r.deals.map((deal) => `<tr>${CTRY_DEAL_COLUMNS.map(([key]) => `<td class="ctry-deal-td ctry-deal-td--${key}">${ctryDealCell(deal, key)}</td>`).join("")}</tr>`).join("")
    : none;
}

function renderCtryTruthFooter(d) {
  const ts = d.truth_status || {};
  const chip = (label, status) => `
    <span class="dash-footer-chip" title="${escapeHtml(`${label}: ${status || "unknown"}`)}">
      <span class="dash-dot ${DASH_STATUS_DOT[status] || "dash-dot--muted"}"></span>${escapeHtml(label)}
    </span>`;
  const allGood = ts.spend === "ready" && ts.fx === "ready" && ts.revenue === "ready"
    && ts.geo_attribution === "ready" && ts.residual === "ready" && ts.deal_proof === "ready";
  return `
    <footer class="dash-truth-footer ${allGood ? "dash-truth-footer--ok" : ""}">
      ${chip("Google Ads spend", ts.spend)}
      ${chip("FX", ts.fx)}
      ${chip("Revenue", ts.revenue)}
      ${chip("Geo attribution", ts.geo_attribution)}
      ${chip("Residual", ts.residual)}
      ${chip("Deal proof", ts.deal_proof)}
      <span class="dash-footer-chip dash-footer-chip--readonly">Read-only</span>
      ${allGood
        ? '<span class="dash-footer-note">Country truth verified — residual is isolated, never distributed.</span>'
        : (dashCanNavigate("revenue-health")
          ? '<a class="dash-footer-note" href="#/revenue-health">Some geo attribution needs review — open Revenue Health</a>'
          : '<span class="dash-footer-note">Some geo attribution needs review — contact your administrator.</span>')}
    </footer>`;
}

// ── Dashboard — Deals tab (PR-ADS-139; one builder since PR-ADS-178B) ──────
//
// The tab reads `/api/dashboard/deals`, which republishes the Pipeline
// builder's figures (services/pipeline_service.build_pipeline) for the
// window, plus a previous-period comparison built by the same builder. It
// renders them with the Pipeline page's own renderers: the KPI cards, the
// closed-won deals by source, the deal list and customers won. Nothing here is
// a second computation: the always-Unavailable opportunity cards, the aging
// panel, the legacy lead-status backlog and the decision cards are gone with
// the composition that produced them (docs/61).

let dashDeals = null;
let dashDealsStatus = "loading";

async function loadDashboardDeals() {
  const window_ = getRoasBusinessWindow();
  const token = ++_revReqSeq.dashDeals;
  setWindowRangeLoading("dashboard-range");

  const root = document.getElementById("dashboard-deals-root");
  const genEl = document.getElementById("dashboard-generated");
  if (root) {
    if (dashDeals && dashDealsStatus === "ok") {
      root.classList.add("is-refreshing");
    } else {
      root.innerHTML = renderDashSkeleton();
    }
  }

  try {
    const data = await fetchJSON(
      `/api/dashboard/deals?window=${encodeURIComponent(window_)}`
    );
    if (token !== _revReqSeq.dashDeals) return;
    if (!_revResponseIsCurrent("dashDeals", token, data)) return;
    dashDeals = data;
    dashDealsStatus = "ok";
    renderWindowRange("dashboard-range", data.window || null);
    if (genEl) genEl.textContent = data.generated_at ? `Generated ${fmtDate(data.generated_at)}` : "";
    renderDashboardDeals();
  } catch (err) {
    console.error("[loadDashboardDeals]", err);
    if (token !== _revReqSeq.dashDeals) return;
    dashDeals = null;
    dashDealsStatus = "error";
    if (genEl) genEl.textContent = "";
    renderWindowRange("dashboard-range", null);
    renderDashboardDeals();
  }
}

function renderDashboardDeals() {
  const root = document.getElementById("dashboard-deals-root");
  if (!root) return;
  root.classList.remove("is-refreshing");

  if (dashDealsStatus !== "ok" || !dashDeals) {
    const nextStep = dashCanNavigate("health")
      ? 'Next step: check <a href="#/health">System Status</a> or retry with Refresh.'
      : "Next step: retry with Refresh, or contact your administrator.";
    root.innerHTML = `
      <div class="revenue-blocked-card dash-error-card">
        <h3 class="revenue-blocked-card__title">Deals &amp; Pipeline unavailable</h3>
        <p>The dashboard deals service could not be reached. Nothing is shown
        because nothing safe could be computed — metrics are never fabricated.</p>
        <p class="revenue-blocked-card__next">${nextStep}</p>
      </div>`;
    return;
  }

  const d = dashDeals;
  const pc = d.period_change || {};
  const compareNote = pc.available
    ? `<p class="dash-compare-note">Change ${escapeHtml(pc.label || "vs previous period")}${pc.note ? ` — ${escapeHtml(pc.note)}` : ""}</p>`
    : `<p class="dash-compare-note dash-compare-note--muted">No previous-period comparison${pc.reason ? ` — ${escapeHtml(pc.reason)}` : ""}</p>`;
  const openPipeline = dashCanNavigate("pipeline")
    ? '<a class="deal-link" href="#/pipeline">Open Pipeline — leads, funnel and deals →</a>' : "";
  root.innerHTML = `
    <div class="dash-tab-intro dash-anim">
      <h2 class="dash-tab-intro__title">Deals</h2>
      <p class="dash-tab-intro__sub">Closed-won deals, customers and won revenue for this window — the Pipeline page's figures, from the same builder. Open and lost deals are not in the deal ledger.</p>
    </div>
    ${pipelineKpiCardsHtml(d, pc)}
    ${compareNote}
    <section class="dash-panel dash-anim deal-source-panel" aria-label="Closed-won deals by source">
      ${pipelineSourceBreakdownHtml(d)}
    </section>
    <section class="dash-panel dash-anim deal-table-panel" aria-label="Closed-won deals" data-pipeline-deals>
      ${pipelineDealsHtml(d)}
    </section>
    <section class="dash-panel dash-anim pipeline-customers-panel" aria-label="Customers won">
      ${pipelineCustomersWonHtml(d)}
    </section>
    <div class="deal-panel-foot">${openPipeline}</div>
    ${pipelineTruthFooterHtml(d)}
  `;
  pipelineWireDeals(root, d);
}

// ── Campaigns page ─────────────────────────────────────────────────────────

// ── The acquisition-cohort gate (PR-ADS-161B, PR-ADS-177A) ─────────────────
// PR-ADS-177C retired the Campaign Evidence page (`/api/campaigns`,
// `/api/campaign-detail`) and its renderers and drawer. Its cohort gate is the
// one gate every leads / SQLs figure still passes through
// (`cohortCountPublication` → `campaignSqlPublication`), so it stays here.

// Watermark phrase for the basis label. Never a fabricated date: an unknown
// watermark says so.
function campaignCohortAsOf(cohort) {
  const md = (cohort && cohort.metadata) || {};
  return md.as_of ? fmtDate(md.as_of) : "an unknown data watermark";
}

// Human sentences for the machine reasons the backend withholds on.
const CAMPAIGN_COHORT_REASONS = {
  canonical_funnel_unreadable: "The canonical contact funnel could not be read, so no cohort SQL count exists for this window. Unavailable is not zero.",
  cohort_reconciliation_failed: "The cohort's attribution buckets did not reconcile, so its SQL count is withheld rather than published unproven.",
  data_watermark_unknown: "The contact-funnel sync state could not be read, so the data watermark these SQLs would be measured at is unknown.",
  request_failed: "Campaign evidence could not be computed for this window.",
  spend_unavailable: "Google Ads spend is unavailable for this window.",
  spend_usd_unavailable_fx_incomplete: "USD spend is unavailable (FX coverage incomplete), so a USD CPQL cannot be computed.",
  cohort_unavailable: "The acquisition cohort is unavailable for this window.",
  source_not_fresh: "The contact-funnel source is stale. Spend is current but these outcomes are not, so CPQL is withheld.",
  source_freshness_unknown: "Whether the contact-funnel source is current cannot be determined, so CPQL is withheld.",
  zero_cohort_sqls: "No contact acquired in this window has reached SQL, so CPQL is not applicable.",
  zero_window_spend: "No Google Ads spend is recorded for this window. Whether none was incurred or none was ingested, there is no cost to divide — CPQL is not $0.",
  // The contact-funnel freshness verdicts that withhold the cohort itself —
  // passed through from analysis/sql_coverage_freshness.py, never collapsed
  // into "stale": each has a different remedy.
  source_sync_state_missing: "The contact funnel has never recorded a sync, so the population these SQLs would be counted over is not proven. A count would be partial, not a total.",
  source_bootstrap_incomplete: "The contact-funnel backfill is still running, so contacts acquired in this window may not all have arrived yet. A count now would be partial, not a total.",
  source_incremental_provenance_missing: "The contact-funnel sync record predates incremental provenance, so it cannot prove the population is current. Withheld until one incremental sync records the evidence.",
  source_last_incremental_failed: "The most recent contact-funnel incremental sync did not complete, so the population is not proven complete. A count now would be partial, not a total.",
  source_no_successful_incremental: "No incremental contact-funnel sync has ever succeeded — a completed backfill is not a live feed — so there is no watermark these SQLs could be measured as of.",
  campaign_attribution_unavailable: "Campaign identity mappings could not be read, so per-campaign SQLs are not reliable enough to divide spend by.",
  no_canonical_spend_row_in_window: "No canonical Google Ads spend row exists for this campaign in this window.",
  unmapped_label_has_no_campaign_spend: "This label has no campaign mapping, so no spend can be divided by its SQLs.",
};
function campaignCohortReason(code) {
  return CAMPAIGN_COHORT_REASONS[code] || (code ? `Withheld (${code}).` : "Withheld.");
}

function campaignSqlPublication(cohort) {
  const c = cohort;
  if (!c || !c.sql_status) {
    return {
      publish: false, state: "unavailable", cpql: false, cpqlState: "unavailable",
      reason: "Acquisition-cohort SQL evidence was not returned for this window, so no SQL total can be shown. Unavailable is not zero.",
      cpqlReason: "No cohort SQL count exists to divide spend by.",
    };
  }
  if (c.sql_status === "published") {
    const cpqlState = c.cpql_status || "unavailable";
    return {
      publish: true, state: "published", reason: null,
      cpql: cpqlState === "published", cpqlState,
      cpqlReason: cpqlState === "published" ? null : campaignCohortReason(c.cpql_reason),
    };
  }
  const state = c.sql_status === "withheld" ? "withheld" : "unavailable";
  return {
    publish: false, state, cpql: false, cpqlState: state,
    reason: campaignCohortReason(c.sql_reason),
    cpqlReason: "CPQL's denominator is the SQL count, which is not published.",
  };
}

// One row's verdict: the page (or drawer) response's verdict, narrowed by the
// row's own copy of it. Both come from the same response; if they ever
// disagree, the stricter one wins — a row can be withheld under a published
// page, never published under a withheld one.
function campaignRowSqlPublication(cohort, row) {
  const page = campaignSqlPublication(cohort);
  if (!page.publish) return page;
  if (!row || row.cohort_sql_status !== "published") {
    return campaignSqlPublication({
      sql_status: (row && row.cohort_sql_status) || "unavailable",
      sql_reason: row && row.cohort_sql_reason,
    });
  }
  return page;
}

// ── PR-ADS-177A — ONE SQL count ──────────────────────────────────────────────
// Every leads / SQLs figure the Revenue Decision Mart publishes — and every
// Dashboard tab and the Countries page that republish it — is the ACQUISITION
// COHORT: Google Ads contacts created in the window, and those proven to have
// reached SQL as of the contact-funnel watermark. Its gate is
// `campaignSqlPublication(cohort)`, and the
// cohort is an ARGUMENT: the `cohort` block of the SAME response the figure
// came from, never page-global state. `row` (optional) narrows it with the
// row's own copy (`sqls_status` / `sqls_reason`) — a row can be withheld under
// a published cohort (the country gate), never published under a withheld one.
const COHORT_SQLS_LABEL = "SQLs · contacts acquired in window";
const COHORT_LEADS_LABEL = "Leads · contacts acquired in window";
const COHORT_COUNT_TITLE = "Acquisition cohort: Google Ads-sourced HubSpot contacts created in this window (Leads), and those canonical lifecycle evidence proves reached SQL as of the contact-funnel watermark (SQLs). The same count on every page.";
function cohortCountPublication(cohort, row) {
  // A row without its own copy of the verdict (a KPI, a total) is gated by
  // the response's cohort alone; a row with one is narrowed by the row
  // helper — the one sanctioned narrowing — never widened.
  if (!row || !row.sqls_status) return campaignSqlPublication(cohort);
  return campaignRowSqlPublication(cohort, {
    cohort_sql_status: row.sqls_status, cohort_sql_reason: row.sqls_reason });
}
// PR-ADS-177A-F1 (truth audit MAJOR 1): a cohort is published over a STALE
// contact funnel only as of its watermark (`docs/44` §7): every mart /
// Dashboard count says so ("measured as of …", `campaignCohortAsOf`) — never a
// bare number.
function cohortIsStale(cohort) {
  const c = cohort || {};
  return c.sql_status === "published"
    && (((c.metadata || {}).source_freshness) || {}).reason === "source_stale";
}
function cohortAsOfNote(cohort) {
  return cohortIsStale(cohort)
    ? ` <small class="metric-reason cohort-as-of">measured as of ${escapeHtml(campaignCohortAsOf(cohort))}</small>`
    : "";
}
// PR-ADS-177A-F1 (truth audit MINOR 1): the last day the cohort counts. The
// campaign and deal views run through today, the country view through
// yesterday — until PR-ADS-177B aligns them, each count says which.
function cohortThrough(cohort) {
  const end = (((cohort || {}).window) || {}).end_date;
  if (!end) return "";
  try {
    return `through ${new Date(`${end}T00:00:00Z`).toLocaleDateString("en-GB", {
      day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" })}`;
  } catch (_) {
    return `through ${end}`;
  }
}
// PR-ADS-177A-F1 (truth audit MINOR 2): a cohort count's period-over-period
// change. An acquisition cohort is still MATURING while its window is open —
// contacts acquired this quarter keep reaching SQL for months — so a current
// quarter / YTD count compared with a closed prior period reads as a fall it
// is not. The chip is shown only for a completed window, and only under a
// published cohort; otherwise the card says why there is no comparison.
function cohortDeltaChip(key, pc, d) {
  const data = d || {};
  if (!cohortCountPublication(data.cohort).publish) return "";
  if (((data.window) || {}).is_closed_window !== true) {
    const note = ((((data.cohort) || {}).metadata) || {}).maturity_note || "";
    return `<span class="dash-delta dash-delta--none cohort-maturing" title="${escapeHtml(note)}">No comparison · window still maturing</span>`;
  }
  return dashDeltaChip(key, pc);
}
function cohortThroughSuffix(cohort) {
  const t = cohortThrough(cohort);
  return t ? ` · ${t}` : "";
}

function cohortReasonText(code) {
  if (!code) return "";
  return CAMPAIGN_COHORT_REASONS[code] || canonicalReasonLabel(code);
}
// A leads / SQLs cell or KPI value: the number only under a published verdict;
// otherwise the withholding word and the reason — never 0, a dash, or the
// number with a suffix.
function cohortCountHtml(value, cohort, row, formatter) {
  const pub = cohortCountPublication(cohort, row);
  if (pub.publish && value !== null && value !== undefined) {
    return escapeHtml((formatter || fmtCount)(value)) + cohortAsOfNote(cohort);
  }
  const r = row || {};
  let code;
  if (pub.publish) code = r.withheld_reason || r.sqls_reason || null;
  else if (r.sqls_status && r.sqls_status !== "published") code = r.sqls_reason;
  else code = (cohort || {}).sql_reason;
  const word = (!pub.publish && pub.state === "withheld") ? "Withheld" : "Unavailable";
  const label = cohortReasonText(code) || pub.reason || "";
  return `<span class="dash-unavailable" title="${escapeHtml(label)}">${word}</span>`
    + (label ? ` <small class="metric-reason">${escapeHtml(label)}</small>` : "");
}
// A KPI's "SQLs" value as plain text for an SVG tooltip (no markup).
function cohortCountText(value, cohort, row) {
  const pub = cohortCountPublication(cohort, row);
  if (pub.publish && value !== null && value !== undefined) {
    return cohortIsStale(cohort) ? `${value} (as of ${campaignCohortAsOf(cohort)})` : String(value);
  }
  return (!pub.publish && pub.state === "withheld") ? "Withheld" : "Unavailable";
}

const CAMPAIGN_WINDOW_LABELS = {
  "7d": "Last 7 days", "14d": "Last 14 days", "30d": "Last 30 days",
  "60d": "Last 60 days", "180d": "Last 180 days", "all_time": "All time",
};

// ── Flagged Waste Terms page — RETIRED (PR-ADS-153D) ───────────────────────
// The standalone page is gone. Its investigation functionality lives in
// Search Terms → Flagged.
// `#/waste` redirects there (see RETIRED_PAGE_REDIRECTS), so old links still
// land on the equivalent view rather than a dead end.
//
// The old loader summed `waste_terms` run snapshots, which double-counted spend
// across repeated runs; the canonical flagged view reads deduplicated
// `search_terms` facts instead. Nothing here is preserved for compatibility —
// keeping a hidden page alive purely for a URL is exactly what §5 forbids.

// ── Pipeline — leads and deals on one page (PR-ADS-178B, docs/61) ──────────
// The Leads page (#/leads) and the Closed-Won Deals page (#/deals) are one
// page, `pipeline`, in the Dashboard design. It reads ONE builder,
// `/api/pipeline` (services/pipeline_service.build_pipeline, one closed-won
// snapshot); the paged contact rows stay on `/api/crm-funnel/contacts`.
// Sections, in order: command bar, KPI cards, the lifecycle funnel, the leads
// list (opened from a funnel stage), the closed-won deal list, customers won,
// a one-line truth footer. Every figure is the server's {value, status,
// reason}; the page decides no publication and sums nothing.
//
// Two "SQL" ideas, never one word: the KPI is the Google Ads ACQUISITION
// COHORT (gated by cohortCountHtml on THIS response's cohort block, as on
// Overview, Campaigns and Dashboard → Deals); the funnel counts contacts that
// ENTERED each lifecycle stage in the window, labelled so.
//
// The Scope and Source selectors are LEAD filters: they narrow the funnel and
// the leads list. They never blank or narrow a deal figure, and Customers won
// is the window's all-source list whatever they are (PR-ADS-178B-F1 m1).

// PR-ADS-166A — the two "customer" figures, each under its own name. They are
// different metrics and are never added, subtracted or shown as one another.
const LEADS_STAGE_MOVE_LABEL = "Moved to Customer stage in HubSpot";
const LEADS_STAGE_MOVE_HELP =
  "Counts contacts whose HubSpot lifecycle stage changed to Customer in this period, "
  + "including old contacts updated by the team. It is not new customers.";
const LEADS_CUSTOMERS_WON_LABEL = "Customers won";
const LEADS_CUSTOMERS_WON_HELP =
  "Distinct companies with a closed-won deal closed in this period.";
// Four different claims about a contact with no name shown, never merged:
//   not_recorded    — HubSpot was asked and holds no name for this contact;
//   not_displayable — HubSpot holds a value in the name field (an email, or
//                     nothing with a letter in it) that is not shown as a name;
//   not_returned    — HubSpot was asked for this contact and did not return it;
//   not_loaded      — the platform has not asked for this contact's name yet.
const LEADS_NAME_NOT_RECORDED = "Name not recorded";
const LEADS_NAME_NOT_LOADED = "Name not loaded yet";
const LEADS_NAME_STATES = {
  not_recorded: { text: LEADS_NAME_NOT_RECORDED,
    title: "HubSpot holds no first or last name for this contact" },
  not_displayable: { text: "Name not shown",
    title: "HubSpot holds a value in the name field that is not shown as a name" },
  not_returned: { text: "Not returned by HubSpot",
    title: "HubSpot was asked for this contact and did not return it — it may have been deleted or merged" },
  not_loaded: { text: LEADS_NAME_NOT_LOADED,
    title: "This contact's name has not been read from HubSpot yet" },
};
const LEADS_WON_LIST_NOT_BUILT = "The list of customers could not be built for this period.";
const LEADS_COMPANY_NAME_LABELS = {
  not_loaded: "Company name not loaded yet",
  not_returned: "Company not returned by HubSpot",
  no_name_in_hubspot: "Company has no name in HubSpot",
  unavailable: "Company name unavailable",
};
const LEADS_COMPANY_NOT_RECORDED = "Company not recorded";

// PR-ADS-182A (owner decision): the Pipeline funnel is the ACQUISITION COHORT
// — contacts created in the window, and how far they have got so far — drawn
// by the one shared block (acquisitionFunnelModel → funnelBlockHtml).
const PIPELINE_FUNNEL_LABEL = "Acquisition cohort";
// The contact LIST's stages (not the funnel's): the list opens on contacts
// that ENTERED a stage in the window, each on its own HubSpot date — the
// label says so ("Entered SQL"), never a bare "SQLs".
const LEADS_STAGES = [
  { event: "lead",        label: "Entered Lead",        stage: "Lead" },
  { event: "mql",         label: "Entered MQL",         stage: "MQL" },
  { event: "sql",         label: "Entered SQL",         stage: "SQL" },
  { event: "opportunity", label: "Entered Opportunity", stage: "Opportunity" },
  // PR-ADS-166A: named for what it counts — contacts whose lifecycle stage was
  // changed to Customer — with its help text. Customers won is a separate
  // figure (pipelineCustomersWonHtml), never derived from this one.
  { event: "customer",    label: LEADS_STAGE_MOVE_LABEL, stage: "Customer" },
];

const LEADS_SCOPES = [
  { key: "all_source",            label: "All Sources" },
  { key: "google_ads_source",     label: "Google Ads-source" },
  { key: "campaign_attributable", label: "Campaign-attributable" },
  { key: "keyword_attributable",  label: "Keyword-attributable" },
];

const LEADS_SOURCE_GROUPS = [
  { key: "",             label: "All sources" },
  { key: "google_ads",   label: "Google Ads" },
  { key: "other_paid",   label: "Other Paid" },
  { key: "organic",      label: "Organic" },
  { key: "offline",      label: "Offline / Other" },
  { key: "unclassified", label: "Unclassified" },
];

// Operational WORKING statuses — deliberately not funnel stages.
const LEADS_STATUS_LABELS = {
  open_working: "Open — being worked",
  sales_qualified_signal: "Sales-qualified signal",
  deal_created_signal: "Deal-created signal",
  disqualified: "Sales disqualified",
  bad_fit: "Bad product fit",
  contact_quality: "Contact quality",
  no_response: "No response",
  discarded: "Discarded",
  reseller: "Reseller",
  no_verdict: "No verdict yet",
  unmapped: "Unmapped value",
};

// The working-status breakdown (operational, not a funnel stage) — the
// Leads page's "Disqualified / Other" view, opened from its own button below
// the funnel, never one of the funnel's stages.
const LEADS_VIEW_OTHER = "other";

const LEADS_PAGE_SIZE = 50;
const PIPELINE_TRUTH_FOOTER = "Open and lost deals are not in the deal ledger: only closed-won "
  + "deals are synced, so open pipeline, lost deals and opportunity aging are not shown here.";

// The stage whose contacts the leads list shows; null until a funnel stage is
// clicked (owner decision 3: the list opens on a stage click).
let _leadsView = null;
let _leadsScope = "all_source";
let _leadsSourceGroup = "";
let _leadsStatus = "";
let _leadsQuery = "";
let _leadsPage = 1;
let _leadsPendingStatus = null;
// The /api/pipeline response being rendered.
let pipelineData = null;
let pipelineStatus = "idle";
let _pipelinePendingSection = null;
// The deal list's own filter (the deal's HubSpot source group — never a lead
// filter), ONE PER SURFACE: the Pipeline page and Dashboard → Deals render the
// same list from two responses, and a group picked on one never filters the
// other (PR-ADS-178C m6). Each selection remembers the window it was made in;
// a different window starts again from every deal source, so a period never
// opens filtered by a choice made for another period.
const _pipelineDealSourceBySurface = {};

/** Carry a retired page's filter intent into the leads list (In Progress Leads). */
function leadsSetPendingStatus(status) {
  _leadsPendingStatus = status || null;
}

/** Carry a retired page's section intent (#/leads, #/deals) into Pipeline. */
function pipelineSetPendingSection(section) {
  _pipelinePendingSection = section || null;
}

function leadsStageFor(event) {
  return LEADS_STAGES.find((s) => s.event === event) || null;
}

async function loadPipeline() {
  // A retired route may have requested a filter (In Progress Leads): the
  // leads list opens on the Lead stage, filtered to that working status.
  if (_leadsPendingStatus) {
    _leadsStatus = _leadsPendingStatus;
    _leadsView = "lead";
    _leadsPendingStatus = null;
    if (!_pipelinePendingSection) _pipelinePendingSection = "leads";
  }
  leadsRenderControls();
  const window_ = getRoasBusinessWindow();
  const token = ++_revReqSeq.pipeline;
  setWindowRangeLoading("pipeline-range");
  const summary = document.getElementById("pipeline-summary");
  if (summary && !(pipelineData && pipelineStatus === "ok")) {
    summary.innerHTML = '<p class="empty-state" style="padding:var(--space-5)">Loading pipeline…</p>';
  }
  const params = new URLSearchParams({ window: window_, scope: _leadsScope });
  if (_leadsSourceGroup) params.set("acquisition_group", _leadsSourceGroup);
  let data = null;
  try {
    data = await fetchJSON(`/api/pipeline?${params.toString()}`);
  } catch (err) {
    console.error("[loadPipeline]", err);
    data = null;
  }
  if (token !== _revReqSeq.pipeline) return;            // superseded
  if (data && !_revResponseIsCurrent("pipeline", token, data)) return;
  pipelineData = data;
  pipelineStatus = data ? "ok" : "error";
  renderWindowRange("pipeline-range", data ? data.window : null);
  renderPipelinePage();
  await leadsRenderActiveView();
  pipelineScrollToPendingSection();
}

function pipelineScrollToPendingSection() {
  const section = _pipelinePendingSection;
  _pipelinePendingSection = null;
  if (!section) return;
  const el = document.getElementById(`pipeline-${section}`);
  if (el && typeof el.scrollIntoView === "function") el.scrollIntoView({ block: "start" });
}

function renderPipelinePage() {
  const summary = document.getElementById("pipeline-summary");
  const outcomes = document.getElementById("pipeline-outcomes");
  if (!summary || !outcomes) return;
  if (pipelineStatus !== "ok" || !pipelineData) {
    summary.innerHTML = `
      <div class="revenue-blocked-card dash-error-card">
        <h3 class="revenue-blocked-card__title">Pipeline unavailable</h3>
        <p>The pipeline service could not be reached. Nothing is shown because nothing
        safe could be computed — figures are never fabricated.</p>
      </div>`;
    outcomes.innerHTML = "";
    return;
  }
  const d = pipelineData;
  summary.innerHTML = `
    ${pipelineKpiCardsHtml(d, null)}
    <section class="dash-panel dash-anim pipeline-funnel-panel" id="pipeline-funnel" aria-label="Lifecycle funnel">
      ${pipelineFunnelHtml(d, _leadsView)}
    </section>`;
  outcomes.innerHTML = `
    <section class="dash-panel dash-anim deal-table-panel" id="pipeline-deals" aria-label="Closed-won deals" data-pipeline-deals>
      ${pipelineDealsHtml(d)}
    </section>
    <section class="dash-panel dash-anim pipeline-customers-panel" id="pipeline-customers" aria-label="Customers won">
      ${pipelineCustomersWonHtml(d)}
    </section>
    ${pipelineTruthFooterHtml(d)}`;
  pipelineWireFunnel(summary);
  pipelineWireDeals(outcomes, d);
}

// ── KPI cards: one source each ──────────────────────────────────────────────
function pipelineMetricPublished(m) {
  return !!m && m.status === "published" && m.value !== null && m.value !== undefined;
}

/** The five KPI cards. `pc` (Dashboard → Deals only) adds the deltas. */
function pipelineKpiCardsHtml(d, pc) {
  const k = (d && d.kpis) || {};
  const opts = campBlockerOpts(d);
  const sqls = k.sqls || {};
  const delta = (key) => (pc ? dashDeltaChip(key, pc) : "");
  const cards = [
    // The acquisition cohort, gated on THIS response's cohort (docs/59).
    { key: "sqls", label: COHORT_SQLS_LABEL,
      value: cohortCountHtml(sqls.value, d.cohort, null),
      sub: `Google Ads contacts · this window${cohortThroughSuffix(d.cohort)}`,
      delta: pc ? cohortDeltaChip("sqls", pc, d) : "",
      source: "HubSpot acquisition cohort",
      // m5 (PR-ADS-178B-F1): the lead filters narrow the funnel and the
      // lead list, never this card — say so where the card is read.
      note: ((d.filters || {}).lead_filtered === true)
        ? "Google Ads acquisition cohort — not narrowed by the lead filters (Scope, Source)." : "",
      ok: cohortCountPublication(d.cohort).publish && sqls.value !== null && sqls.value !== undefined },
    { key: "deals", label: "Closed-won deals",
      value: canonicalMetricHtml(k.closed_won_deals, fmtCount, opts),
      sub: "Close date in this window · every source", delta: delta("closed_won_deals"),
      source: "HubSpot Closed-Won", ok: pipelineMetricPublished(k.closed_won_deals) },
    // Customers are distinct HubSpot companies — never a deal count, never summed.
    { key: "customers", label: "Customers",
      value: canonicalMetricHtml(k.customers, fmtCount, opts),
      sub: "Distinct HubSpot companies · never summed", delta: delta("closed_won_customers"),
      source: "HubSpot companies on closed-won deals", ok: pipelineMetricPublished(k.customers) },
    { key: "revenue", label: "Won revenue",
      value: canonicalMetricHtml(k.won_revenue_usd, fmtMoney, opts),
      sub: "HubSpot closed-won USD", delta: delta("closed_won_revenue_usd"),
      source: "HubSpot Closed-Won", ok: pipelineMetricPublished(k.won_revenue_usd) },
    { key: "average", label: "Average deal value",
      value: canonicalMetricHtml(k.average_deal_value_usd, fmtMoney, opts),
      sub: "Won revenue ÷ closed-won deals", delta: "",
      source: "HubSpot Closed-Won", ok: pipelineMetricPublished(k.average_deal_value_usd) },
  ];
  return `
    <div class="dash-kpi-grid pipeline-kpi-grid">
      ${cards.map((c) => `
        <div class="dash-kpi-card dash-anim ${c.ok ? "" : "dash-kpi-card--unavailable"}" data-kpi="${c.key}">
          <div class="dash-kpi-card__label">${escapeHtml(c.label)}</div>
          <div class="dash-kpi-card__value">${c.value}</div>
          <div class="dash-kpi-card__sub">${escapeHtml(c.sub)}</div>
          ${c.note ? `<div class="dash-kpi-card__note" data-kpi-note>${escapeHtml(c.note)}</div>` : ""}
          ${c.delta ? `<div class="dash-kpi-card__meta">${c.delta}</div>` : ""}
          <div class="dash-kpi-card__source">${escapeHtml(c.source)}</div>
        </div>`).join("")}
    </div>`;
}

// ── The lifecycle funnel: the acquisition cohort (PR-ADS-182A) ──────────────
/**
 * The Pipeline funnel panel: the SAME acquisition-cohort block the Overview,
 * Campaigns, Countries and Channels draw (acquisitionFunnelModel →
 * funnelBlockHtml), read from THIS response's `funnel` alone. Below it, the
 * contact list's own stage picker: that list is a different count (contacts
 * that ENTERED a stage in the window) and says so; the funnel's columns are
 * not buttons onto it. `activeEvent` is the list's selection — an ARGUMENT.
 */
function pipelineFunnelHtml(d, activeEvent) {
  const f = (d && d.funnel) || {};
  const lf = f.lead_filters || {};
  const published = ((f.publication || {}).status === "published") && !!f.totals;
  const asOf = published && f.stale && f.as_of
    ? ` <small class="metric-reason cohort-as-of">measured as of ${escapeHtml(fmtDate(f.as_of))}</small>` : "";
  const scopeLabel = lf.scope_label || "All sources";
  const groupLabel = lf.acquisition_group_label || "All sources";
  return `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Lifecycle funnel · ${escapeHtml(PIPELINE_FUNNEL_LABEL)}</h3>
        <p class="dash-panel__sub">${escapeHtml(PIPELINE_FUNNEL_DESCRIPTION)} ${escapeHtml(pipelineFunnelPopulationText(f))}${asOf}</p>
      </div>
    </div>
    ${funnelBlockHtml(pipelineFunnelModel(f), { ariaLabel: PIPELINE_FUNNEL_LABEL })}
    <p class="lifecycle-legend">${escapeHtml(ACQUISITION_FUNNEL_LEGEND)}</p>
    <div class="pipeline-list-picker" role="group" aria-label="Contact list">
      <p class="grace-note pipeline-list-picker__note">${escapeHtml(PIPELINE_LIST_NOTE)}</p>
      <div class="pipeline-other-view">
        ${LEADS_STAGES.map((st) => `<button type="button" class="search-terms-tab${activeEvent === st.event ? " active" : ""}"
          data-leads-stage="${escapeHtml(st.event)}" aria-pressed="${activeEvent === st.event ? "true" : "false"}"
          title="${escapeHtml(st.event === "customer" ? LEADS_STAGE_MOVE_HELP
            : `List the contacts that entered ${st.stage} in this window`)}">${escapeHtml(st.label)}</button>`).join("")}
        <button type="button" class="search-terms-tab search-terms-tab--muted${activeEvent === LEADS_VIEW_OTHER ? " active" : ""}"
          data-leads-view="${LEADS_VIEW_OTHER}">Disqualified / Other — working status, not a funnel stage</button>
      </div>
    </div>
    <p class="grace-note">Lead filters — Scope: ${escapeHtml(scopeLabel)} · Source: ${escapeHtml(groupLabel)} ·
      they narrow the funnel and the contact list, never the SQL card or a deal figure.</p>`;
}

const PIPELINE_FUNNEL_DESCRIPTION = "Contacts created in these days, and how far they have got so far.";
const PIPELINE_LIST_NOTE = "Contact list — a different count from the funnel above: "
  + "contacts that ENTERED a stage in this window, each on its own HubSpot "
  + "stage-entry date. Pick a stage to list them.";
// The reasons a lead filter cannot be honoured on the cohort basis (the
// server names them, `funnel.lead_filters.unavailable_reason`).
const PIPELINE_FUNNEL_REASONS = {
  lead_scope_not_on_cohort_basis: "the acquisition cohort is not split by campaign or keyword attribution, so this Scope cannot be applied to it; choose All sources or Google Ads-source (the contact list below still applies this Scope)",
  lead_filters_select_no_contact: "Scope Google Ads-source and this Source select no contact together: a contact has one source group",
  acquisition_funnel_counts_missing: "this response carries no cohort count for the selected population",
};

/** Whose contacts the funnel counts, in words, from the payload. */
function pipelineFunnelPopulationText(f) {
  const lf = (f && f.lead_filters) || {};
  if (lf.honoured === false) return "";
  const group = lf.source_group;
  if (!group) {
    return "All sources. The SQL card above is the Google Ads acquisition cohort only, and is not narrowed by the lead filters.";
  }
  return `Source group: ${sourceGroupLabel(group)} (HubSpot Original Source and its drill-down) — part of the same cohort. The SQL card above is the Google Ads acquisition cohort and is not narrowed by the lead filters.`;
}

/**
 * PR-ADS-182A — the Pipeline funnel's model: the shared acquisition adapter
 * over `d.funnel`, exactly as Overview's (no rate: `shares: false`; a contact
 * with no created date makes every column "at least N"). Nothing here
 * divides or decides publication.
 */
function pipelineFunnelModel(f) {
  const lf = (f && f.lead_filters) || {};
  return acquisitionFunnelModel(f, {
    head: "Contacts acquired",
    headSub: lf.source_group ? `created in this window · ${sourceGroupLabel(lf.source_group)}`
      : "created in this window · all sources",
    reasonText: (code) => PIPELINE_FUNNEL_REASONS[code] || cohortFunnelReasonText(code),
    note: lf.honoured === false ? "No figure is shown for this filter." : undefined,
    shares: false,
  });
}

function pipelineWireFunnel(root) {
  (root || document).querySelectorAll("[data-leads-view]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      _leadsView = LEADS_VIEW_OTHER;
      _leadsPage = 1;
      (root || document).querySelectorAll("[data-leads-stage]").forEach((b) => {
        b.classList.remove("active");
        b.setAttribute("aria-pressed", "false");
      });
      btn.classList.add("active");
      await leadsRenderActiveView();
      const leads = document.getElementById("pipeline-leads");
      if (leads && typeof leads.scrollIntoView === "function") leads.scrollIntoView({ block: "start" });
    });
  });
  (root || document).querySelectorAll("[data-leads-stage]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      _leadsView = btn.getAttribute("data-leads-stage");
      _leadsPage = 1;
      (root || document).querySelectorAll("[data-leads-stage]").forEach((b) => {
        const on = b === btn;
        b.classList.toggle("active", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
      (root || document).querySelectorAll("[data-leads-view]").forEach((b) => b.classList.remove("active"));
      await leadsRenderActiveView();
      const leads = document.getElementById("pipeline-leads");
      if (leads && typeof leads.scrollIntoView === "function") leads.scrollIntoView({ block: "start" });
    });
  });
}

// ── The closed-won deal list ────────────────────────────────────────────────
// The deal's stored HubSpot source group. An identifier this map does not know
// is shown as stored, never folded into another group.
const DEAL_SOURCE_GROUP_LABELS = {
  google_ads: "Google Ads",
  other_paid: "Other Paid",
  organic: "Organic",
  offline: "Offline",
  unclassified: "Unclassified",
  ambiguous: "Ambiguous — contacts disagree",
  not_recorded: "Source not recorded",
};

function dealSourceLabel(group) {
  if (group === null || group === undefined || group === "") return DEAL_SOURCE_GROUP_LABELS.not_recorded;
  return DEAL_SOURCE_GROUP_LABELS[group] || String(group).replace(/_/g, " ");
}

function dealSourceKey(row) {
  return (row && row.acquisition_group) || "not_recorded";
}

// One deal's own USD amount. An unknown amount is never zero and never a dash.
function dealAmountHtml(r) {
  if (r.deal_amount_usd === null || r.deal_amount_usd === undefined) {
    return `${dashValue(null)} <small class="metric-reason">no proven USD amount</small>`;
  }
  return escapeHtml(fmtMoney(r.deal_amount_usd));
}

/**
 * The deal rows the page may show, or null. ONLY beside a published
 * closed-won population, and only when the list numbers exactly that count —
 * the server says so (`deals_status`) and the page checks it again: a list
 * that disagrees with its own headline is never shown beside it.
 */
function pipelineDealRows(d) {
  const data = d || {};
  const pop = closedWonPopulation(data) || {};
  const count = (data.kpis || {}).closed_won_deals || {};
  if (data.deals_status !== "published" || !Array.isArray(data.deals)) return null;
  // The PR-ADS-164A rule: published, and exactly that many deals arrived.
  if (closedWonPopulationState(pop, data.deals.length) === "blocked") return null;
  // The KPI card beside the list must state the same count.
  if (count.status !== "published" || count.value !== pop.value) return null;
  return data.deals;
}

/** Why the list is not shown: the server's own verdict for it, else the
 *  population's (closedWonBlockedMetric reads a published one as "the rows
 *  received do not match the published count"). */
function pipelineDealListVerdict(d) {
  const data = d || {};
  if (data.deals_status && data.deals_status !== "published") {
    return { value: null, status: data.deals_status, reason: data.deals_reason };
  }
  return closedWonPopulation(data);
}

// Close date newest first, then the larger amount (owner decision 7).
function pipelineSortDeals(rows) {
  return [...rows].sort((a, b) =>
    String(b.deal_close_date || "").localeCompare(String(a.deal_close_date || "")) ||
    (b.deal_amount_usd || 0) - (a.deal_amount_usd || 0));
}

/** The deal list filtered on the deal's OWN source group ("" = every deal). */
function pipelineFilterDeals(rows, group) {
  if (!group) return rows;
  return rows.filter((r) => dealSourceKey(r) === group);
}

/** Which surface a payload is rendered on: Dashboard → Deals says so
 *  (`surface: "dashboard_deals"`); everything else is the Pipeline page. */
function pipelineDealSurface(d) {
  return (d && d.surface === "dashboard_deals") ? "dashboard_deals" : "pipeline";
}

function pipelineWindowKey(d) {
  return String(((d && d.window) || {}).key || "");
}

/** This surface's selected deal source group for THIS response's window, or
 *  "" — a selection made in another window is not carried over. */
function pipelineDealSourceFor(d) {
  const sel = _pipelineDealSourceBySurface[pipelineDealSurface(d)];
  if (!sel || sel.window !== pipelineWindowKey(d)) return "";
  return sel.group || "";
}

function pipelineSetDealSource(d, group) {
  _pipelineDealSourceBySurface[pipelineDealSurface(d)] = {
    group: group || "", window: pipelineWindowKey(d) };
}

/**
 * The deal-source filter's options, from the payload's OWN per-group counts
 * (`source_breakdown`: each deal's own group through the reader contract) —
 * one source for the figures, never a second count of the rows here. A group
 * whose count is not published says so; with no breakdown there is no filter.
 */
function pipelineDealSourceOptions(d) {
  const groups = (d && d.source_breakdown) || null;
  if (!Array.isArray(groups) || !groups.length) return null;
  return groups
    .map((g) => {
      const key = g.source_group || "not_recorded";
      const m = (g.metrics || {}).closed_won_deals || {};
      const published = m.status === "published" && m.value !== null && m.value !== undefined;
      return { key, label: dealSourceLabel(key === "not_recorded" ? null : key),
        count: published ? m.value : null };
    })
    .sort((a, b) => a.label.localeCompare(b.label));
}

function pipelineDealsHtml(d) {
  const header = `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Closed-won deals</h3>
        <p class="dash-panel__sub">Every HubSpot closed-won deal closed in this window, from every source · newest close first · click a deal for detail</p>
      </div>
    </div>`;
  const rows = pipelineDealRows(d);
  if (!rows) {
    // The PR-ADS-164A word and reason (closedWonBlockedHtml): the server's
    // own verdict for the list, else the population's — a published count
    // the list contradicts reads "the deal rows received do not match".
    return `${header}
      <div class="revenue-blocked-card">
        <div class="revenue-blocked-card__eyebrow">Closed-won deals</div>
        <h3>${closedWonBlockedHtml(pipelineDealListVerdict(d))}</h3>
        <p>The deal list is shown only beside a published closed-won count, and only when it lists exactly that many deals. This does not mean that no deals closed.</p>
      </div>`;
  }
  if (!rows.length) {
    return `${header}
      <div class="revenue-blocked-card revenue-empty-card">
        <div class="revenue-blocked-card__eyebrow">Closed-won deals</div>
        <h3>No closed-won deals closed in this period</h3>
        <p>The deal ledger was read and is current. No HubSpot deal, from any source, was closed-won in this business window.</p>
      </div>`;
  }
  // The counts are the payload's: the card's published deal count for "All",
  // and `source_breakdown` per group (m6) — the page counts no rows here.
  const groups = pipelineDealSourceOptions(d);
  const chosen = pipelineDealSourceFor(d);
  const active = (groups && groups.some((g) => g.key === chosen)) ? chosen : "";
  const total = ((d && d.kpis) || {}).closed_won_deals || {};
  const countText = (n) => (n === null || n === undefined
    ? "count not published" : fmtCount(n));
  const options = groups
    ? [`<option value="">All deal sources (${escapeHtml(countText(total.value))})</option>`]
      .concat(groups.map((g) =>
        `<option value="${escapeHtml(g.key)}"${g.key === active ? " selected" : ""}>${escapeHtml(g.label)} (${escapeHtml(countText(g.count))})</option>`))
      .join("")
    : "";
  const shown = pipelineSortDeals(pipelineFilterDeals(rows, active));
  const body = shown.map((r) => `
    <tr class="deal-row" data-pipeline-deal="${escapeHtml(String(r.deal_id))}" tabindex="0" role="button" aria-label="Open ${escapeHtml(r.deal_name || "deal")} detail">
      <td class="deal-td--name">${escapeHtml(r.deal_name || "—")}</td>
      <td>${escapeHtml(r.deal_close_date ? String(r.deal_close_date).slice(0, 10) : "—")}</td>
      <td class="deal-num">${dealAmountHtml(r)}</td>
      <td>${escapeHtml(dealSourceLabel(r.acquisition_group))}</td>
      <td>${escapeHtml(r.campaign_name || "—")}</td>
      <td>${escapeHtml(r.country || "—")}</td>
    </tr>`).join("");
  const note = active
    ? `<p class="deal-stage-note" data-pipeline-deal-filter-note>Showing ${escapeHtml(fmtCount(shown.length))} of ${escapeHtml(fmtCount(rows.length))} closed-won deals — filtered by each deal's own HubSpot source group. The cards above count every deal.</p>`
    : "";
  const controls = groups ? `
    <div class="roas-window-controls pipeline-deal-controls">
      <label class="roas-window-label" for="${pipelineDealSurface(d)}-deal-source">Deal source:</label>
      <select id="${pipelineDealSurface(d)}-deal-source" class="waste-filter-select" data-pipeline-deal-source aria-label="Filter deals by the deal's own source group">${options}</select>
    </div>` : "";
  return `${header}${controls}
    <div class="deal-scroll">
      <table class="deal-table deal-table--rows pipeline-deal-table">
        <thead><tr><th>Deal</th><th>Closed</th><th>Won Revenue</th><th>Source</th><th>Campaign</th><th>Country</th></tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>
    ${note}`;
}

// The deal drawer shows only fields the ledger carries for a deal. Company,
// company id, main contact, created date, age and days to close are not on
// the ledger (they were always empty) and are not listed.
const PIPELINE_DEAL_DRAWER_FIELDS = [
  ["deal_name", "Deal"], ["deal_id", "HubSpot deal ID"],
  ["deal_amount_usd", "Won revenue (USD)"], ["deal_close_date", "Close date"],
  ["deal_stage_label", "Stage"], ["acquisition_group", "Source group"],
  ["campaign_name", "Campaign label"], ["country", "Country"],
  ["attribution_status", "Attribution"], ["attribution_scope", "Attribution scope"],
];

function pipelineDealDrawerCell(r, key) {
  if (key === "deal_amount_usd") return dealAmountHtml(r);
  if (key === "acquisition_group") return escapeHtml(dealSourceLabel(r.acquisition_group));
  if (key === "deal_close_date" && r.deal_close_date) return escapeHtml(String(r.deal_close_date).slice(0, 10));
  const v = r[key];
  if (v === null || v === undefined || v === "") return '<span class="muted">Not recorded</span>';
  return escapeHtml(String(v));
}

function pipelineDealDrawerHtml(r) {
  const tiles = PIPELINE_DEAL_DRAWER_FIELDS.map(([key, label]) =>
    `<div><dt>${escapeHtml(label)}</dt><dd>${pipelineDealDrawerCell(r, key)}</dd></div>`).join("");
  return `
    <div class="deal-drawer__head">
      <strong>${escapeHtml(r.deal_name || "Deal")}</strong>
      <button type="button" class="deal-drawer__close" aria-label="Close">×</button>
    </div>
    <div class="deal-drawer__section">
      <h4 class="deal-drawer__h">Closed-won deal</h4>
      <dl class="deal-drawer__grid">${tiles}</dl>
    </div>`;
}

function pipelineWireDeals(root, d) {
  const panel = (root || document).querySelector("[data-pipeline-deals]");
  if (!panel) return;
  const rows = pipelineDealRows(d) || [];
  const sel = panel.querySelector("[data-pipeline-deal-source]");
  if (sel) {
    sel.addEventListener("change", (e) => {
      pipelineSetDealSource(d, e.target.value || "");
      panel.innerHTML = pipelineDealsHtml(d);
      pipelineWireDeals(root, d);
    });
  }
  panel.querySelectorAll("[data-pipeline-deal]").forEach((tr) => {
    const open = () => {
      const r = rows.find((x) => String(x.deal_id) === tr.getAttribute("data-pipeline-deal"));
      if (!r) return;
      let drawer = panel.querySelector(".deal-drawer");
      if (!drawer) {
        drawer = document.createElement("div");
        drawer.className = "deal-drawer";
        panel.appendChild(drawer);
      }
      drawer.innerHTML = pipelineDealDrawerHtml(r);
      const close = drawer.querySelector(".deal-drawer__close");
      if (close) close.addEventListener("click", () => drawer.remove());
      if (typeof drawer.scrollIntoView === "function") drawer.scrollIntoView({ block: "nearest" });
    };
    tr.addEventListener("click", open);
    tr.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });
  });
}

// ── Customers won ───────────────────────────────────────────────────────────
/**
 * The won customers, as a list — ONLY when the customer count is published and
 * the list has exactly that many entries. Withheld means absent: no list, no
 * partial list, no placeholder rows. Never merged with the deal list.
 */
function pipelineCustomersWonRows(d) {
  const data = d || {};
  const cw = data.customers_won || {};
  const m = (data.kpis || {}).customers || {};
  if (cw.status !== "published" || !Array.isArray(cw.rows)) return null;
  if (m.status !== "published" || cw.rows.length !== m.value) return null;
  return cw.rows;
}

function pipelineCustomersWonHtml(d) {
  const header = `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">${escapeHtml(LEADS_CUSTOMERS_WON_LABEL)}</h3>
        <p class="dash-panel__sub">${escapeHtml(LEADS_CUSTOMERS_WON_HELP)} Not the same as contacts moved to the Customer stage.</p>
      </div>
    </div>`;
  const rows = pipelineCustomersWonRows(d);
  if (!rows) {
    const cw = (d && d.customers_won) || {};
    const verdict = cw.status === "published"
      ? { value: null, status: "withheld", reason: "closed_won_rows_do_not_match_count" }
      : { value: null, status: cw.status || "unavailable", reason: cw.reason || "closed_won_verdict_missing" };
    return `${header}<p class="deal-stage-note" data-customers-won-withheld>${canonicalMetricHtml(verdict, null, campBlockerOpts(d))}
      — no list of customers is shown.</p>`;
  }
  if (!rows.length) {
    return `${header}<p class="rev-breakdown-empty">No customer was won in this period.</p>`;
  }
  const body = rows.map((r) => {
    const name = r.company_name_status === "named" ? String(r.company_name || "").trim() : "";
    const cell = name
      ? `<strong>${escapeHtml(name)}</strong>`
      : `<span class="muted">${escapeHtml(LEADS_COMPANY_NAME_LABELS[r.company_name_status] || LEADS_COMPANY_NAME_LABELS.not_loaded)}</span>`;
    const closed = r.latest_close_date ? escapeHtml(String(r.latest_close_date).slice(0, 10)) : `<span class="muted">Unavailable</span>`;
    return `<tr><td>${cell}</td><td class="deal-num">${dashValue(r.closed_won_deals, fmtCount)}</td><td>${closed}</td></tr>`;
  }).join("");
  return `${header}
    <div class="deal-scroll">
      <table class="deal-table pipeline-customers-table">
        <thead><tr><th title="The name of the HubSpot company on the won deal">Customer</th><th>Closed-won deals</th><th>Latest close date</th></tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>`;
}

// ── Dashboard → Deals only: the deal list by each deal's own source group ───
function pipelineSourceBreakdownHtml(d) {
  const header = `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Closed-won deals by source</h3>
        <p class="dash-panel__sub">Each deal's own HubSpot source group · customers are distinct companies per source and are not added up</p>
      </div>
    </div>`;
  const rows = (d && d.source_breakdown) || null;
  if (!Array.isArray(rows)) {
    return `${header}<p class="deal-stage-note">${canonicalMetricHtml(pipelineDealListVerdict(d), null, campBlockerOpts(d))}</p>`;
  }
  if (!rows.length) return `${header}<p class="rev-breakdown-empty">No closed-won deals in this window.</p>`;
  const opts = campBlockerOpts(d);
  const body = rows.map((r) => `
    <tr>
      <td class="deal-td--name">${escapeHtml(dealSourceLabel(r.source_group === "not_recorded" ? null : r.source_group))}</td>
      <td class="deal-num">${rowMetricHtml(r, "closed_won_deals", fmtCount, opts)}</td>
      <td class="deal-num">${rowMetricHtml(r, "customers", fmtCount, opts)}</td>
      <td class="deal-num">${rowMetricHtml(r, "won_revenue", fmtMoney, opts)}</td>
    </tr>`).join("");
  return `${header}
    <div class="deal-scroll">
      <table class="deal-table"><thead><tr><th>Source</th><th>Won deals</th><th>Customers</th><th>Won revenue</th></tr></thead><tbody>${body}</tbody></table>
    </div>`;
}

function pipelineTruthFooterHtml(d) {
  return `
    <footer class="dash-truth-footer pipeline-truth-footer">
      <span class="dash-footer-chip dash-footer-chip--readonly">Read-only</span>
      <span class="dash-footer-note">${escapeHtml((d && d.truth_footer) || PIPELINE_TRUTH_FOOTER)}</span>
    </footer>`;
}

// ── The leads list (section 4): paged rows from /api/crm-funnel/contacts ───
function leadsRenderControls() {
  const scopeSel = document.getElementById("leads-scope");
  if (scopeSel && !scopeSel.options.length) {
    scopeSel.innerHTML = LEADS_SCOPES.map(
      (s) => `<option value="${s.key}">${escapeHtml(s.label)}</option>`).join("");
    scopeSel.value = _leadsScope;
    scopeSel.addEventListener("change", async (e) => {
      // A lead filter: the funnel and the list. Deal figures do not change.
      _leadsScope = e.target.value; _leadsPage = 1;
      await loadPipeline();
    });
  }
  const sourceSel = document.getElementById("leads-source");
  if (sourceSel && !sourceSel.options.length) {
    sourceSel.innerHTML = LEADS_SOURCE_GROUPS.map(
      (g) => `<option value="${g.key}">${escapeHtml(g.label)}</option>`).join("");
    sourceSel.value = _leadsSourceGroup;
    sourceSel.addEventListener("change", async (e) => {
      // Reloads the FUNNEL as well as the rows — one selected source, one
      // lead population, everywhere it applies.
      _leadsSourceGroup = e.target.value; _leadsPage = 1;
      await loadPipeline();
    });
  }
  const statusSel = document.getElementById("leads-status");
  if (statusSel && !statusSel.options.length) {
    const opts = [`<option value="">All working statuses</option>`].concat(
      Object.keys(LEADS_STATUS_LABELS).map(
        (k) => `<option value="${k}">${escapeHtml(LEADS_STATUS_LABELS[k])}</option>`));
    statusSel.innerHTML = opts.join("");
    statusSel.addEventListener("change", async (e) => {
      _leadsStatus = e.target.value; _leadsPage = 1;
      await leadsRenderActiveView();
    });
  }
  if (statusSel) statusSel.value = _leadsStatus;

  const search = document.getElementById("leads-search");
  if (search && !search.dataset.wired) {
    search.dataset.wired = "1";
    let t = null;
    search.addEventListener("input", (e) => {
      clearTimeout(t);
      const value = e.target.value;
      t = setTimeout(async () => {
        _leadsQuery = value; _leadsPage = 1;
        await leadsRenderActiveView();
      }, 300);
    });
  }
}

async function leadsRenderActiveView() {
  const controls = document.getElementById("leads-table-controls");
  const pager = document.getElementById("leads-pagination");
  const header = document.getElementById("leads-table-header");
  const body = document.getElementById("leads-table-body");
  leadsSyncControlVisibility();
  if (_leadsView === LEADS_VIEW_OTHER) {
    if (controls) controls.hidden = false;
    if (pager) pager.innerHTML = "";
    await leadsRenderOperational();
    return;
  }
  const stage = leadsStageFor(_leadsView);
  if (!stage) {
    // Owner decision 3: the list opens on a click on a funnel stage.
    if (controls) controls.hidden = true;
    if (pager) pager.innerHTML = "";
    if (header) header.textContent = "Leads";
    if (body) body.innerHTML = '<p class="empty-state" style="padding: var(--space-5);">Pick a stage under the funnel above to list the contacts that entered it in this window.</p>';
    return;
  }
  if (controls) controls.hidden = false;
  await leadsRenderContacts(stage.event);
}

/**
 * Show only the filters the ACTIVE view can honour.
 *
 * The Disqualified / Other view is an aggregate breakdown BY working status, so
 * a working-status filter would be circular and a company search has nothing
 * per-contact to match. Both are hidden there rather than left visible and
 * inert. Window, Scope and Source stay — they all genuinely filter that view.
 */
function leadsSyncControlVisibility() {
  const operational = _leadsView === LEADS_VIEW_OTHER;
  const statusControl = document.getElementById("leads-status-control");
  const searchControl = document.getElementById("leads-search-control");
  const note = document.getElementById("leads-controls-note");
  if (statusControl) statusControl.hidden = operational;
  if (searchControl) searchControl.hidden = operational;
  if (note) {
    note.hidden = !operational;
    note.textContent = operational
      ? "This view IS the working-status breakdown, so the status and company filters do not apply here. Window, scope and source do."
      : "";
  }
}

/**
 * A working-status count: null/undefined is UNAVAILABLE — the word, never 0
 * and never a dash (PR-ADS-178B-F1 MAJOR-1: restored; the operational view
 * calls it).
 */
function leadsCount(value) {
  return (value === null || value === undefined)
    ? '<span class="dash-unavailable">Unavailable</span>'
    : escapeHtml(Number(value).toLocaleString());
}

async function leadsRenderOperational() {
  const header = document.getElementById("leads-table-header");
  const body = document.getElementById("leads-table-body");
  if (header) header.textContent = "Working status — operational, not a funnel stage";
  if (!body) return;
  body.innerHTML = `<p class="empty-state" style="padding: var(--space-5);">Loading…</p>`;

  // The Scope and Source selectors stay visible on this view, so both must
  // actually filter it — and with the same semantics the funnel strip above
  // uses, or these counts would describe a broader population than their own
  // headline.
  const params = new URLSearchParams({
    window: getRoasBusinessWindow(),
    window_type: "business",
    event: "lead",
    scope: _leadsScope,
  });
  if (_leadsSourceGroup) params.set("acquisition_group", _leadsSourceGroup);
  let payload = null;
  try {
    payload = await fetchJSON(`/api/crm-funnel/operational-status?${params.toString()}`);
  } catch (_) { payload = null; }

  if (!payload || payload.available === false) {
    const reason = payload && payload.reason === "campaign_identity_unavailable"
      ? "Google Ads campaign identity is unavailable, so this attribution scope cannot be resolved. This is unavailable — not zero."
      : "Working-status breakdown unavailable.";
    body.innerHTML = `<p class="empty-state" style="padding: var(--space-5);">${escapeHtml(reason)}</p>`;
    return;
  }
  const counts = payload.counts || {};
  const rows = Object.keys(LEADS_STATUS_LABELS).map((key) => `<tr>
      <td>${escapeHtml(LEADS_STATUS_LABELS[key])}</td>
      <td><code>${escapeHtml(key)}</code></td>
      <td>${leadsCount(counts[key] === undefined ? 0 : counts[key])}</td>
    </tr>`).join("");

  body.innerHTML = `<table class="data-table">
      <thead><tr><th>Working status</th><th>Category</th><th>Contacts</th></tr></thead>
      <tbody>${rows}</tbody></table>
    <p class="grace-note" style="padding: 0 var(--space-5) var(--space-4);">
      These are MDR working statuses inside the MQL process — they do not define any funnel stage.
      Counted over contacts whose Lead stage-entry date falls in the window.
      Scope: ${escapeHtml(payload.scope_label || "All sources")} ·
      Source: ${escapeHtml(payload.acquisition_group_label || "All sources")}.</p>`;
}

/**
 * A contact's name from HubSpot, or the words saying why there is none.
 * Each status says its own words (LEADS_NAME_STATES). A status this page does
 * not know — or none — is the weakest claim: not loaded yet.
 */
function leadsNameState(row) {
  const status = row ? row.contact_name_status : null;
  return (typeof status === "string" && Object.prototype.hasOwnProperty.call(LEADS_NAME_STATES, status))
    ? LEADS_NAME_STATES[status] : LEADS_NAME_STATES.not_loaded;
}

function leadsContactNameHtml(row) {
  const status = row ? row.contact_name_status : null;
  const name = status === "recorded" ? String(row.contact_name || "").trim() : "";
  if (name) return `<strong>${escapeHtml(name)}</strong>`;
  const state = leadsNameState(row);
  return `<span class="muted" title="${escapeHtml(state.title)}">${escapeHtml(state.text)}</span>`;
}

/**
 * The note under the Contact column header while names are still loading.
 * Driven by the database counts in `names_progress`; shown for as long as any
 * stored contact has NEVER been asked for, however many already are. A contact
 * HubSpot was asked for and did not return is not "still loading", so the note
 * clears when the load is truly done. Unknown counts say so.
 */
function leadsNamesProgressNote(progress) {
  const p = progress || {};
  const known = p.available === true && Number.isFinite(p.read)
    && Number.isFinite(p.total) && Number.isFinite(p.never_asked);
  if (!known) return "Names are being loaded from HubSpot: progress is unavailable.";
  if (p.never_asked <= 0) return "";
  return `Names are being loaded from HubSpot: ${Number(p.read).toLocaleString()} of ${Number(p.total).toLocaleString()} contacts read so far.`;
}

function leadsNamesNoteHtml(progress) {
  const note = leadsNamesProgressNote(progress);
  return note ? `<small class="metric-reason leads-names-note">${escapeHtml(note)}</small>` : "";
}

/** The Company field typed on the HubSpot contact — not a deal's company record. */
function leadsCompanyHtml(row) {
  const company = String((row && row.company) || "").trim();
  return company
    ? escapeHtml(company)
    : `<span class="muted">${LEADS_COMPANY_NOT_RECORDED}</span>`;
}

/** Plain-text heading for a contact (drawer title): name, else company. */
function leadsContactHeading(row) {
  const status = row ? row.contact_name_status : null;
  const name = status === "recorded" ? String(row.contact_name || "").trim() : "";
  const company = String((row && row.company) || "").trim();
  if (name && company) return `${name} — ${company}`;
  return name || company || leadsNameState(row).text;
}

function sourceGroupLabel(group) {
  const found = LEADS_SOURCE_GROUPS.find((g) => g.key === group);
  return found ? found.label : (group || "—");
}

/** Canonical acquisition channel + platform, from the shared source taxonomy. */
function leadsChannelPlatformHtml(row) {
  const channel = row.source_channel_label || "—";
  const platform = row.source_platform_label || "—";
  if (channel === platform) return escapeHtml(channel);
  return `${escapeHtml(channel)} <span class="muted">· ${escapeHtml(platform)}</span>`;
}

/**
 * Campaign / keyword cell.
 *
 * `hs_analytics_source_data_1` / `_2` are HubSpot Original Source Drill-Down
 * fields. They mean "Google Ads campaign / keyword" ONLY for Paid Search
 * contacts. For Organic, Paid Social, Email, Referral, Offline and Event
 * contacts they hold different text entirely, so this NEVER labels that text
 * Campaign or Keyword — the row's canonical source columns carry that evidence
 * instead, and the neutral drill-down value is available in the drawer.
 */
function leadsCampaignCellHtml(row, field) {
  if (row.campaign_available) return escapeHtml(row[field] || "—");
  switch (row.campaign_semantics) {
    case "not_google_ads_source":
      return `<span class="muted" title="Not a Google Ads contact — HubSpot's source drill-down is not a Google Ads ${field}. See the Source and Channel / Platform columns.">Not applicable</span>`;
    case "campaign_identity_unavailable":
      return `<span class="muted" title="The Google Ads campaign-identity contract could not be consulted. Unavailable — not absent.">Unavailable</span>`;
    case "campaign_identity_unresolved":
      return `<span class="muted" title="This label did not resolve to a real Google Ads campaign identity.">Unresolved</span>`;
    default:
      return `<span class="muted">—</span>`;
  }
}

/** Contact drawer — funnel history, current CRM state, acquisition evidence. */
function leadsOpenDrawer(row, stage) {
  if (!row) return;
  const stageRows = LEADS_STAGES.map((s) => {
    const value = (row.stage_dates || {})[s.event];
    const cell = value
      ? escapeHtml(value)
      : `<span class="muted" title="HubSpot supplied no stage-entry timestamp">Unavailable — no HubSpot stage-entry evidence</span>`;
    const isSelected = s.event === stage.event ? " selected-stage" : "";
    return `<tr class="${isSelected}"><td>${escapeHtml(s.label)}</td><td>${cell}</td></tr>`;
  }).join("");

  // Google Ads campaign / keyword are shown ONLY where Google Ads semantics are
  // proven. Everything else states why, and the neutral drill-down value below
  // carries the contact's real source evidence without mislabelling it.
  let attribution;
  if (row.campaign_available) {
    attribution = `<h4>Google Ads attribution</h4>
      <table class="data-table"><tbody>
        <tr><td>Campaign</td><td>${escapeHtml(row.campaign || "—")}</td></tr>
        <tr><td>Keyword</td><td>${escapeHtml(row.keyword || "—")}</td></tr>
      </tbody></table>`;
  } else if (row.campaign_semantics === "not_google_ads_source") {
    attribution = `<h4>Google Ads attribution</h4>
      <p class="muted">Not applicable — this is not a Google Ads contact.
      HubSpot's source drill-down for this contact is not a Google Ads campaign or
      keyword, so it is reported above as canonical source evidence rather than
      labelled as one.</p>`;
  } else if (row.campaign_semantics === "campaign_identity_unresolved") {
    attribution = `<h4>Google Ads attribution</h4>
      <p class="muted">This contact's campaign label did not resolve to a real
      Google Ads campaign identity, so no campaign or keyword is claimed.</p>`;
  } else {
    attribution = `<h4>Google Ads attribution</h4>
      <p class="muted">Unavailable — the Google Ads campaign-identity contract
      could not be consulted. Unavailable is not absent.</p>`;
  }

  const html = `
    <h3>${escapeHtml(leadsContactHeading(row))}</h3>
    <p class="grace-note">Selected event: <strong>${escapeHtml(stage.label)}</strong> on
      ${escapeHtml(row.event_date || "—")} · Current lifecycle:
      <strong>${escapeHtml(row.lifecycle_stage_label || "—")}</strong></p>

    <h4>Funnel history</h4>
    <table class="data-table"><tbody>${stageRows}</tbody></table>

    <h4>Current CRM state</h4>
    <table class="data-table"><tbody>
      <tr><td>Lifecycle stage</td><td>${escapeHtml(row.lifecycle_stage_label || "—")}</td></tr>
      <tr><td>MQL status</td><td>${escapeHtml(row.mql_status || "—")}</td></tr>
      <tr><td>Working category</td><td>${escapeHtml(LEADS_STATUS_LABELS[row.mql_status_category] || row.mql_status_category || "—")}</td></tr>
      <tr><td>Owner</td><td>${escapeHtml(row.owner_id || "—")}</td></tr>
      <tr><td>Contact name</td><td>${leadsContactNameHtml(row)}</td></tr>
      <tr><td title="The Company field on the HubSpot contact">Company</td><td>${leadsCompanyHtml(row)}</td></tr>
      <tr><td>HubSpot contact ID</td><td><code>${escapeHtml(row.contact_id || "—")}</code></td></tr>
    </tbody></table>

    <h4>Acquisition evidence</h4>
    <table class="data-table"><tbody>
      <tr><td>Acquisition group</td><td>${escapeHtml(row.acquisition_group_label || sourceGroupLabel(row.acquisition_group))}</td></tr>
      <tr><td>Channel</td><td>${escapeHtml(row.source_channel_label || "—")}</td></tr>
      <tr><td>Platform</td><td>${escapeHtml(row.source_platform_label || "—")}</td></tr>
      <tr><td>Raw HubSpot source</td><td>${escapeHtml(row.acquisition_source_raw || "—")}</td></tr>
      <tr><td title="HubSpot Original Source Drill-Down — a Google Ads campaign only for Paid Search contacts">Source detail</td><td>${escapeHtml(row.source_detail_raw || "—")}</td></tr>
      <tr><td>Country</td><td>${escapeHtml(row.country || "—")}</td></tr>
      <tr><td>GCLID</td><td>${row.has_gclid ? "Present" : "Absent"}</td></tr>
    </tbody></table>
    ${attribution}`;

  const overlay = document.getElementById("leads-drawer-overlay");
  const drawer = document.getElementById("leads-drawer");
  const titleEl = document.getElementById("leads-drawer-title");
  const bodyEl = document.getElementById("leads-drawer-body");
  if (!overlay || !drawer || !bodyEl) return;
  overlay.hidden = false; overlay.setAttribute("aria-hidden", "false");
  drawer.hidden = false;
  if (titleEl) titleEl.textContent = leadsContactHeading(row);
  bodyEl.innerHTML = html;
  overlay.onclick = closeLeadsDrawer;
  const closeBtn = document.getElementById("leads-drawer-close-btn");
  if (closeBtn) closeBtn.onclick = closeLeadsDrawer;
  _leadsDrawerKeyHandler = (e) => { if (e.key === "Escape") closeLeadsDrawer(); };
  document.addEventListener("keydown", _leadsDrawerKeyHandler);
}

let _leadsDrawerKeyHandler = null;

function closeLeadsDrawer() {
  const overlay = document.getElementById("leads-drawer-overlay");
  const drawer = document.getElementById("leads-drawer");
  if (overlay) { overlay.hidden = true; overlay.setAttribute("aria-hidden", "true"); }
  if (drawer) { drawer.hidden = true; }
  if (_leadsDrawerKeyHandler) {
    document.removeEventListener("keydown", _leadsDrawerKeyHandler);
    _leadsDrawerKeyHandler = null;
  }
}

async function leadsRenderContacts(event) {
  const stage = leadsStageFor(event);
  const header = document.getElementById("leads-table-header");
  const body = document.getElementById("leads-table-body");
  const pager = document.getElementById("leads-pagination");
  if (header) header.textContent = `Contacts that entered the ${stage.stage} stage in this window`;
  if (!body) return;
  body.innerHTML = `<p class="empty-state" style="padding: var(--space-5);">Loading contacts…</p>`;

  const params = new URLSearchParams({
    window: getRoasBusinessWindow(),
    window_type: "business",
    event,
    scope: _leadsScope,
    page: String(_leadsPage),
    page_size: String(LEADS_PAGE_SIZE),
  });
  if (_leadsSourceGroup) params.set("acquisition_group", _leadsSourceGroup);
  if (_leadsStatus) params.set("operational_status", _leadsStatus);
  if (_leadsQuery) params.set("q", _leadsQuery);

  let payload = null;
  try {
    payload = await fetchJSON(`/api/crm-funnel/contacts?${params.toString()}`);
  } catch (_) { payload = null; }

  if (!payload) {
    body.innerHTML = `<p class="empty-state" style="padding: var(--space-5);">Could not load contacts.</p>`;
    if (pager) pager.innerHTML = "";
    return;
  }
  if (payload.available === false) {
    const reason = payload.reason === "campaign_identity_unavailable"
      ? "Google Ads campaign identity is unavailable, so this attribution scope cannot be resolved. This is unavailable — not zero."
      : "Canonical CRM contact store unavailable.";
    body.innerHTML = `<p class="empty-state" style="padding: var(--space-5);">${escapeHtml(reason)}</p>`;
    if (pager) pager.innerHTML = "";
    return;
  }

  const rows = payload.rows || [];
  if (!rows.length) {
    body.innerHTML = `<p class="empty-state" style="padding: var(--space-5);">No contacts entered the ${escapeHtml(stage.stage)} stage in this window.</p>`;
    if (pager) pager.innerHTML = "";
    return;
  }

  const tbody = rows.map((r, i) => {
    return `<tr class="clickable-row" data-leads-row="${i}" tabindex="0">
      <td>${leadsContactNameHtml(r)}</td>
      <td>${leadsCompanyHtml(r)}</td>
      <td>${escapeHtml(r.event_date || "—")}</td>
      <td>${escapeHtml(r.lifecycle_stage_label || "—")}</td>
      <td>${escapeHtml(LEADS_STATUS_LABELS[r.mql_status_category] || r.mql_status_category || "—")}</td>
      <td>${escapeHtml(r.acquisition_group_label || sourceGroupLabel(r.acquisition_group))}</td>
      <td>${leadsChannelPlatformHtml(r)}</td>
      <td>${leadsCampaignCellHtml(r, "campaign")}</td>
      <td>${leadsCampaignCellHtml(r, "keyword")}</td>
      <td>${escapeHtml(r.country || "—")}</td>
      <td>${r.has_gclid ? "Yes" : "No"}</td>
    </tr>`;
  }).join("");

  body.innerHTML = `<div class="deal-scroll"><table class="data-table">
    <thead><tr>
      <th title="The contact's first and last name in HubSpot">Contact${leadsNamesNoteHtml(payload.names_progress)}</th>
      <th title="The Company field on the HubSpot contact">Company</th>
      <th title="The selected stage-entry event date">${escapeHtml(stage.stage)} stage-entry date</th>
      <th title="Current lifecycle stage — may be later than the selected event">Current stage</th>
      <th>Working status</th>
      <th>Source</th>
      <th title="Canonical acquisition channel and platform">Channel / Platform</th>
      <th title="Google Ads campaign — only shown where Google Ads semantics are proven">Campaign</th>
      <th title="Google Ads keyword — only shown where Google Ads semantics are proven">Keyword</th>
      <th>Country</th><th>GCLID</th>
    </tr></thead>
    <tbody>${tbody}</tbody></table></div>`;

  body.querySelectorAll("[data-leads-row]").forEach((tr) => {
    const open = () => leadsOpenDrawer(rows[Number(tr.getAttribute("data-leads-row"))], stage);
    tr.addEventListener("click", open);
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
  });

  if (pager) {
    const total = payload.total;
    const from = (payload.page - 1) * payload.page_size + 1;
    const to = from + rows.length - 1;
    pager.innerHTML = `
      <button type="button" class="btn btn--secondary" id="leads-prev" ${payload.page <= 1 ? "disabled" : ""}>Previous</button>
      <span class="roas-window-range">${from}–${to}${total === null ? "" : ` of ${Number(total).toLocaleString()}`}</span>
      <button type="button" class="btn btn--secondary" id="leads-next" ${payload.has_more ? "" : "disabled"}>Next</button>`;
    const prev = document.getElementById("leads-prev");
    const next = document.getElementById("leads-next");
    if (prev) prev.addEventListener("click", async () => { _leadsPage = Math.max(1, _leadsPage - 1); await leadsRenderContacts(event); });
    if (next) next.addEventListener("click", async () => { _leadsPage += 1; await leadsRenderContacts(event); });
  }
}

// ── Revenue Attribution Health — admin diagnostics (PR-ADS-113) ─────────────
// Read-only data health for revenue decisions, backed by the existing audit
// endpoint. This is diagnostics, not a decision page. Critical doctrine: a SAFE
// audit verdict ALONE does not mean Revenue Ready — revenue is ready only when
// the audit verdict is SAFE AND revenue attribution is wired.

function revenueHealthCheckChip(label, ok, okText, warnText) {
  return `<div class="revenue-status-chip revenue-status-chip--${ok ? "ok" : "warning"}">
    <span>${label}</span><strong>${ok ? okText : warnText}</strong>
  </div>`;
}

function renderRevenueHealth(audit) {
  const dg = audit.date_grain_health || {};
  const wc = audit.window_comparison || {};
  const pollutionCount =
    (audit.pseudo_campaign_rows || []).length + (audit.email_campaign_rows || []).length;

  const ranges = audit.window_ranges || {};
  const cmpKeys = ["current_quarter", "ytd", "all_time"];

  // PR-ADS-177A-F2 (truth audit round 2, MAJOR): the window's leads and SQLs
  // are the acquisition cohort. Its verdict is its OWN check — a withheld
  // cohort withholds leads and SQLs with the reason and blocks no page (owner
  // decision, docs/59 §3) — so it is never part of revenue readiness. The
  // legacy `leads` table's date grain feeds no figure and is not judged here.
  const leadCohort = audit.lead_cohort || {};
  const cohortPublished = leadCohort.published === true;
  // PR-ADS-161E: the canonical closed-won population (not a legacy
  // gclid_attribution check) vs selected-window status.
  const integrationConnected = audit.canonical_revenue_status === "canonical_available";
  const windowHasRevenue = (audit.revenue_window_status
    ? audit.revenue_window_status === "has_revenue"
    : audit.revenue_attribution_wired === true);
  const pollutionOk = pollutionCount === 0;

  // Window integrity is a property of the resolved DATE BOUNDARIES, never the
  // aggregates: identical Current Quarter / YTD / All Time totals are correct
  // when all available data falls inside the current quarter. We only flag a
  // genuine bug where the windows fail to resolve to distinct date ranges.
  const rangeSignatures = cmpKeys.map((k) => {
    const r = ranges[k] || {};
    return `${r.start_date}|${r.end_date}`;
  });
  const windowIntegrityOk = !audit.window_ranges || new Set(rangeSignatures).size > 1;

  // Revenue truth is ready when the audit is SAFE and the integration is
  // connected. A connected integration whose selected window has no
  // closed-won deals is a SAFE EMPTY state — never "not wired".
  const revenueReady = audit.verdict === "SAFE" && integrationConnected;

  const blockers = (audit.blockers || []).slice();
  if (!integrationConnected) {
    blockers.push("Revenue integration is not connected: no attributed closed-won deals exist yet.");
  }

  // The next action points at a REAL admin workflow — never a nonexistent
  // generic lead re-import action.
  let nextAction = "Revenue truth is ready — no action needed.";
  if (!integrationConnected) {
    nextAction = "Run Revenue Truth Recovery (below) to connect closed-won attribution, then re-check this page.";
  }
  // PR-ADS-177A-F3 (truth audit round 3, MINOR 1): pseudo / email rows in the
  // legacy `leads` table are reported (the chip below), never a next step for
  // revenue: no figure reads that table.

  const readinessCard = revenueReady
    ? `<div class="revenue-blocked-card revenue-empty-card">
         <div class="revenue-blocked-card__eyebrow">Revenue readiness</div>
         <h3>Revenue Ready</h3>
         <p>The audit is SAFE and the revenue integration is connected.${!windowHasRevenue ? " The selected window has no closed-won deals yet — a safe empty state." : ""}</p>
       </div>`
    : `<div class="revenue-blocked-card">
         <div class="revenue-blocked-card__eyebrow">Revenue readiness</div>
         <h3>Revenue is not ready</h3>
         <p>Revenue truth is ready only when the audit is SAFE and the revenue integration is connected. (A connected integration with no closed-won deals in the selected window is safe-empty, not a blocker.)</p>
         <ul class="revenue-health-blockers">${blockers.map((b) => `<li>${escapeHtml(b)}</li>`).join("")}</ul>
         <div class="revenue-next-step"><strong>Next step:</strong> ${escapeHtml(nextAction)}</div>
       </div>`;

  const fmtRange = (r) => {
    if (!r || (!r.start_date && !r.end_date)) return "—";
    return `${r.start_date || "Earliest"} → ${r.end_date || "—"}`;
  };

  const cmpRow = (k, label) => {
    const s = wc[k] || {};
    // PR-ADS-177A-F2: the window's leads are its acquisition cohort, through
    // the one gate with THAT window's own cohort verdict.
    const leads = cohortCountHtml(s.leads, (audit.window_cohorts || {})[k], null);
    return `<tr>
      <td>${label}</td>
      <td class="revenue-health-range">${escapeHtml(fmtRange(ranges[k]))}</td>
      <td>${fmtMoney(s.spend)}</td>
      <td>${leads}</td>
      <td>${fmtMoney(s.won_revenue)}</td>
    </tr>`;
  };

  return `
    <div class="revenue-health-note">A SAFE audit verdict alone does not mean Revenue Ready.</div>

    <div class="revenue-status-grid revenue-health-checks">
      ${revenueHealthCheckChip("Leads / SQLs (acquisition cohort)", cohortPublished,
        // PR-ADS-177A-F3: the selected window's own cohort (every window the
        // selector offers), its last day and — over a stale funnel — its
        // as-of watermark.
        `Published${escapeHtml(cohortThroughSuffix(leadCohort.cohort))}${cohortAsOfNote(leadCohort.cohort)}`,
        `${leadCohort.status === "withheld" ? "Withheld" : "Unavailable"} — ${escapeHtml(cohortReasonText(leadCohort.reason) || "reason unavailable")}`)}
      ${revenueHealthCheckChip("Revenue integration", integrationConnected, "Connected", "Not connected")}
      ${revenueHealthCheckChip("Selected window", true, windowHasRevenue ? "Revenue rows" : "No closed-won revenue (safe)", "")}
      ${revenueHealthCheckChip("Campaign pollution", pollutionOk, "Clean", `${pollutionCount} legacy leads-table row(s) · read by no figure`)}
      ${revenueHealthCheckChip("Window integrity", windowIntegrityOk, "Windows differ", "Windows identical")}
    </div>

    ${cohortPublished ? "" : `<p class="revenue-health-note revenue-health-cohort-step">Leads / SQLs: withheld separately, never a revenue blocker.${COHORT_LEADS_WITHHELD_STEP}</p>`}

    ${readinessCard}

    <div class="revenue-health-rails">
      <h3>Source date rails</h3>
      <ul>
        <li><span>Spend</span><code>${escapeHtml(dg.spend_date_field || "geo.run_date")}</code></li>
        <li><span>Leads</span><code>${escapeHtml(dg.lead_date_field_current || "acquisition cohort · contact_created_at")}</code></li>
        <li><span>Revenue</span><code>${escapeHtml(dg.deal_date_field || "gclid_attribution.deal_close_date")}</code></li>
      </ul>
    </div>

    <div class="revenue-summary-strip revenue-summary-strip--four">
      <div><span>Included paid leads missing dates</span><strong>${fmtCountOrUnknown(audit.missing_contact_created_at_count)}</strong></div>
      <div><span>Legacy paid rows excluded from revenue truth</span><strong>${fmtCountOrUnknown(audit.legacy_excluded_count)}</strong></div>
      <div><span>Excluded non-paid rows</span><strong>${fmtCountOrUnknown(audit.excluded_non_paid_count)}</strong></div>
      <div><span>Excluded pseudo campaigns</span><strong>${fmtCountOrUnknown(audit.excluded_pseudo_campaign_count)}</strong></div>
    </div>

    <div class="panel">
      <div class="panel__header">Window comparison</div>
      <div class="panel__body panel__body--flush">
        <div class="table-scroll">
          <table class="data-table">
            <tr><th>Window</th><th>Date range</th><th>Spend</th><th>Leads</th><th>Won Revenue</th></tr>
            <tbody>
              ${cmpRow("current_quarter", "Current Quarter")}
              ${cmpRow("ytd", "YTD")}
              ${cmpRow("all_time", "All Time")}
            </tbody>
          </table>
        </div>
      </div>
    </div>
    <p class="revenue-footnote">These windows differ by their date boundaries (shown above). Their totals may legitimately be equal when all available data falls within the current quarter.</p>
  `;
}

// PR-ADS-126: compact, canonical "Revenue Page Parity" panel. Answers the
// acceptance-criteria question — do all Revenue & Attribution pages agree with
// the mart on spend / revenue, and if not, exactly why? Driven solely by
// GET /api/revenue-performance/audit; the frontend computes no parity itself.
function renderRevenuePageParity(audit) {
  if (!audit || !Array.isArray(audit.pages)) return "";
  const fmtVal = (v) => (v === null || v === undefined ? "Unavailable" : fmtMoney(v));
  const fmtDiff = (v) => (v === null || v === undefined ? "—" : fmtMoney(v));
  const statusBadge = (s) => {
    const cls = s === "pass" ? "ok" : (s === "fail" ? "warning" : "neutral");
    return `<span class="parity-status parity-status--${cls}">${escapeHtml(s || "—")}</span>`;
  };
  const rows = audit.pages.map((p) => `
    <tr>
      <td>${escapeHtml(p.page || p.page_key || "—")}</td>
      <td>${escapeHtml(p.metric || "—")}</td>
      <td>${fmtVal(p.current_value)}</td>
      <td>${fmtVal(p.mart_value)}</td>
      <td>${fmtDiff(p.difference)}</td>
      <td>${statusBadge(p.status)}</td>
      <td>${escapeHtml((p.reasons || []).join(", ") || (p.status === "pass" ? "—" : ""))}</td>
    </tr>
  `).join("");
  // PR-ADS-128: a compact, scannable summary above the table. Rendered purely
  // from the audit response (no frontend parity computation): the headline agree
  // verdict plus an explicit list of any drifting pages and their reasons.
  const allAgree = audit.all_pages_agree === true;
  const drift = audit.pages.filter((p) => p.status !== "pass");
  const driftHtml = (!allAgree && drift.length)
    ? `<div class="revenue-parity-drift">
         <span>Pages with drift</span>
         <ul>${drift.map((p) => `<li>${escapeHtml(p.page || p.page_key || "—")} — ${escapeHtml((p.reasons || []).join(", ") || p.status || "drift")}</li>`).join("")}</ul>
       </div>`
    : "";
  return `
    <div class="panel revenue-page-parity">
      <div class="panel__header">Revenue Page Parity</div>
      <div class="panel__body panel__body--flush">
        <div class="revenue-parity-summary revenue-parity-summary--${allAgree ? "ok" : "warning"}">
          <div class="revenue-parity-summary__verdict">Campaign, Country and Deals pages agree with the mart: <strong>${allAgree ? "Yes" : "No"}</strong></div>
          ${driftHtml}
        </div>
        <div class="revenue-table-shell">
          <div class="revenue-table-scroll table-scroll">
            <table class="data-table roas-table revenue-decision-table revenue-parity-table">
              <tr><th>Page</th><th>Metric</th><th>Current</th><th>Mart</th><th>Difference</th><th>Status</th><th>Reason</th></tr>
              <tbody>${rows}</tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  `;
}

async function loadRevenueHealth() {
  // Google Ads Spend Truth panel (PR-ADS-118) sits at the top of diagnostics.
  loadSpendTruth();
  // Google Ads Geo Sync + country reconciliation (PR-ADS-124).
  loadGeoSync();
  // Campaign identity mapping review (PR-ADS-119).
  loadCampaignMapping();
  // Admin-only Revenue Recovery panel (PR-ADS-114) sits above the diagnostics.
  loadRevenueRecoveryStatus();
  // Admin-only Source Attribution Backfill panel (PR-ADS-168B: moved here from
  // the deleted Revenue by Source page).
  renderSourceBackfillPanelInit();

  const body = document.getElementById("revenue-health-body");
  if (!body) return;
  body.innerHTML = '<p class="empty-state" style="padding:var(--space-5)">Loading revenue attribution health…</p>';
  const window_ = getRoasBusinessWindow();
  const token = ++_revReqSeq.health;
  setWindowRangeLoading("revenue-health-range");
  try {
    const res = await fetch(`/api/revenue-attribution/audit?window=${encodeURIComponent(window_)}`, { credentials: "same-origin" });
    const audit = res.ok ? await res.json() : null;
    if (token !== _revReqSeq.health) return;  // stale response superseded
    if (!res.ok || !_revResponseIsCurrent("health", token, audit)) {
      if (!res.ok) {
        body.innerHTML = '<p class="empty-state" style="padding:var(--space-5)">Revenue attribution health is unavailable right now.</p>';
      }
      return;
    }
    renderWindowRange("revenue-health-range", audit.window || null);
    // PR-ADS-126: prepend the canonical Revenue Page Parity panel (best-effort —
    // the diagnostics body still renders if the mart audit is briefly down).
    let parityHtml = "";
    try {
      const martAuditRes = await fetch(
        `/api/revenue-performance/audit?window=${encodeURIComponent(window_)}`,
        { credentials: "same-origin" }
      );
      if (martAuditRes.ok) {
        const martAudit = await martAuditRes.json();
        if (token === _revReqSeq.health) parityHtml = renderRevenuePageParity(martAudit);
      }
    } catch (e) { /* parity panel is best-effort diagnostics */ }
    if (token !== _revReqSeq.health) return;
    body.innerHTML = parityHtml + renderRevenueHealth(audit);
    // Surface the included missing-event-date count on the reconciliation panel.
    loadLeadReconciliationStatus(audit.missing_contact_created_at_count);
  } catch (err) {
    console.error("[loadRevenueHealth]", err);
    if (token !== _revReqSeq.health) return;
    body.innerHTML = '<p class="empty-state" style="padding:var(--space-5)">Error loading revenue attribution health.</p>';
  }
}

// ── Revenue Truth Recovery panel (PR-ADS-114, admin-only) ───────────────────
// Reads HubSpot read-only and writes ONLY to the local DB; never writes to any
// external platform. Dry Run reports expected impact; Run Recovery executes.

let revenueRecoveryJobId = null;
let revenueRecoveryPollTimer = null;
let revenueRecoveryNotice = "";
const REVENUE_RECOVERY_POLL_MS = 2500;  // poll durable status every ~2.5s

function renderRecoveryResult(status) {
  const s = (status && status.summary) || null;
  if (!s) return "";
  const mode = status.dry_run ? "Dry run (no writes)" : "Recovery";
  const rows = [
    ["Paid contacts recovered", s.paid_contacts_recovered],
    ["Contacts still missing createdate", s.contacts_still_missing_createdate],
    ["Closed-won deals found", s.closed_won_deals_found],
    ["Closed-won deals with GCLID", s.closed_won_deals_with_gclid],
    ["Attributed deal rows written", s.attributed_deal_rows_written],
    ["Closed-won deals without GCLID", s.closed_won_deals_without_gclid],
    ["Deals without campaign mapping", s.deals_without_campaign_mapping],
  ];
  const errorsHtml = (status.errors && status.errors.length)
    ? `<div class="revenue-next-step"><strong>Errors:</strong><ul class="revenue-health-blockers">${status.errors.map((e) => `<li>${escapeHtml(String(e))}</li>`).join("")}</ul></div>`
    : "";
  return `
    <div class="recovery-result">
      <div class="recovery-result__head">${escapeHtml(mode)} · ${escapeHtml(status.date_from || "")} → ${escapeHtml(status.date_to || "")} · <strong>${escapeHtml(status.status || "")}</strong></div>
      <div class="revenue-summary-strip revenue-summary-strip--four">
        ${rows.map(([label, val, fmt]) => `<div><span>${label}</span><strong>${(fmt || fmtCount)(val)}</strong></div>`).join("")}
      </div>
      ${errorsHtml}
    </div>
  `;
}

function renderRevenueRecoveryPanel(status) {
  const panel = document.getElementById("revenue-recovery-panel");
  if (!panel) return;
  const st = status || {};
  const running = st.running === true;
  const hasSummary = !!st.summary;

  const contactsStatus = hasSummary
    ? `${st.summary.paid_contacts_recovered || 0} recovered`
    : "Not yet run";
  const attributionStatus = hasSummary
    ? `${st.summary.attributed_deal_rows_written || 0} attributed rows`
    : "Not yet run";

  const progressHtml = running
    ? `<div class="recovery-progress">Running… phase: <strong>${escapeHtml(st.phase || "starting")}</strong>${st.current_chunk ? ` · chunk ${escapeHtml(st.current_chunk)}` : ""}${(st.completed_chunks && st.completed_chunks.length) ? ` · ${st.completed_chunks.length} chunk(s) done` : ""}</div>`
    : "";
  const noticeHtml = revenueRecoveryNotice
    ? `<p class="revenue-footnote">${escapeHtml(revenueRecoveryNotice)}</p>`
    : "";

  panel.innerHTML = `
    <div class="panel recovery-panel">
      <div class="panel__header">Revenue Truth Recovery</div>
      <div class="panel__body">
        <div class="recovery-statuses">
          <div>Historical paid contacts: <strong>${escapeHtml(contactsStatus)}</strong></div>
          <div>Closed-won attribution: <strong>${escapeHtml(attributionStatus)}</strong></div>
        </div>
        <p class="revenue-footnote">Runs in the background. Reads HubSpot read-only and writes only to the local database; never writes to HubSpot or Google Ads, and never fabricates attribution. Default range: All Time.</p>
        <div class="recovery-actions">
          <button type="button" class="btn btn--secondary" data-recovery-action="dry-run" ${running ? "disabled" : ""}>Dry Run</button>
          <button type="button" class="btn btn--primary" data-recovery-action="run" ${running ? "disabled" : ""}>Run Recovery</button>
        </div>
        ${progressHtml}
        ${noticeHtml}
        ${renderRecoveryResult(st)}
      </div>
    </div>
  `;
}

function stopRevenueRecoveryPolling() {
  if (revenueRecoveryPollTimer) {
    clearTimeout(revenueRecoveryPollTimer);
    revenueRecoveryPollTimer = null;
  }
}

async function fetchRevenueRecoveryStatus() {
  const url = revenueRecoveryJobId
    ? `/api/revenue-recovery/status?job_id=${encodeURIComponent(revenueRecoveryJobId)}`
    : "/api/revenue-recovery/status";
  const res = await fetch(url, { credentials: "same-origin" });
  if (!res.ok) return null;
  return res.json();
}

async function pollRevenueRecoveryOnce() {
  // Only keep polling while the user is on the Revenue Health page.
  if (_currentPage !== "revenue-health") { stopRevenueRecoveryPolling(); return; }
  try {
    const status = await fetchRevenueRecoveryStatus();
    if (status) {
      if (status.job_id) revenueRecoveryJobId = status.job_id;
      renderRevenueRecoveryPanel(status);
      if (status.running) {
        revenueRecoveryPollTimer = setTimeout(pollRevenueRecoveryOnce, REVENUE_RECOVERY_POLL_MS);
        return;
      }
    }
  } catch (err) {
    console.error("[pollRevenueRecovery]", err);
  }
  stopRevenueRecoveryPolling();
}

async function loadRevenueRecoveryStatus() {
  const panel = document.getElementById("revenue-recovery-panel");
  if (!panel) return;
  stopRevenueRecoveryPolling();
  revenueRecoveryNotice = "";
  try {
    const status = await fetchRevenueRecoveryStatus();
    if (!status) { renderRevenueRecoveryPanel(null); return; }
    if (status.job_id) revenueRecoveryJobId = status.job_id;
    renderRevenueRecoveryPanel(status);
    // If a job is already running (e.g. started before a reload), resume polling.
    if (status.running) {
      revenueRecoveryPollTimer = setTimeout(pollRevenueRecoveryOnce, REVENUE_RECOVERY_POLL_MS);
    }
  } catch (err) {
    console.error("[loadRevenueRecoveryStatus]", err);
    renderRevenueRecoveryPanel(null);
  }
}

async function runRevenueRecoveryJob(dryRun) {
  revenueRecoveryNotice = "";
  renderRevenueRecoveryPanel({ running: true, phase: dryRun ? "dry-run" : "starting" });
  try {
    const res = await fetch("/api/revenue-recovery/run", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: dryRun }),
    });
    if (res.status === 409) {
      revenueRecoveryNotice = "A recovery job is already running.";
      loadRevenueRecoveryStatus();
      return;
    }
    if (!res.ok) {
      revenueRecoveryNotice = "Revenue recovery could not be started.";
      renderRevenueRecoveryPanel(null);
      return;
    }
    // 202 Accepted — the job runs in the background; poll its durable status.
    const accepted = await res.json();
    revenueRecoveryJobId = accepted.job_id || null;
    stopRevenueRecoveryPolling();
    pollRevenueRecoveryOnce();
  } catch (err) {
    console.error("[runRevenueRecoveryJob]", err);
    revenueRecoveryNotice = "Revenue recovery request failed.";
    renderRevenueRecoveryPanel(null);
  }
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-recovery-action]");
  if (!btn) return;
  runRevenueRecoveryJob(btn.dataset.recoveryAction === "dry-run");
});

// ── Google Ads Geo Sync panel (PR-ADS-124, admin-only) ──────────────────────
// Country ROAS needs geo (per-country) spend to reconcile with the canonical
// campaign-level Google Ads spend. This panel shows the reconciliation status
// and lets an admin Preview / Run the geo sync. Reads Google Ads read-only and
// writes ONLY the local canonical geo table — never Google Ads, never HubSpot.

let geoSyncJobId = null;
let geoSyncPollTimer = null;
let geoSyncNotice = "";
let geoSyncReconcile = null;
const GEO_SYNC_POLL_MS = 2500;

function geoSyncStatusBadge(status) {
  const map = {
    reconciled: ["revenue-status-chip--ok", "Reconciled"],
    // PR-ADS-131: a safe unblock — reconciled with an explicit unattributed residual.
    reconciled_with_residual: ["revenue-status-chip--ok", "Reconciled with residual"],
    mismatch: ["revenue-status-chip--warning", "Mismatch"],
    no_geo_data: ["revenue-status-chip--warning", "No geo data yet"],
    unavailable: ["revenue-status-chip--warning", "Unavailable"],
  };
  const [cls, label] = map[status] || map.unavailable;
  return `<div class="revenue-status-chip ${cls}"><span>Geo reconciliation status</span><strong>${label}</strong></div>`;
}

// PR-ADS-129: the reconciliation tolerance is a FRACTION (0.02) — render it as a
// true percentage ("2%") so it reads consistently with variance_pct (already ×100)
// and is never mistaken for 0.02%.
function fmtTolerancePct(fraction) {
  if (fraction === null || fraction === undefined) return "—";
  return `${Math.round(fraction * 10000) / 100}%`;
}

// PR-ADS-129: map the backend geo-reconciliation reason/next_action codes to
// human copy. The frontend renders the backend's verdict — it never recomputes it.
function geoReconcileCauseText(reason) {
  switch (reason) {
    case "reconciled": return "Geo and campaign spend reconcile within tolerance.";
    case "missing_geo_dates": return "Some dates have campaign spend but no geo rows — the geo sync did not cover those dates.";
    case "campaign_spend_without_geo": return "One or more campaigns spent but have NO geo rows at all — the geo report is missing those campaigns.";
    case "unattributed_location_spend":
    case "geo_report_does_not_reconcile_by_design":
      return "Geo rows exist for every campaign-spend day and campaign, but the geo total is still below the campaign total. The residual is Google Ads spend with no identifiable location — the geographic_view report omits it, so this denominator cannot reconcile to campaign spend under the current query.";
    case "geo_total_below_campaign_total": return "Geo total is below the campaign total.";
    default: return "Geo and campaign totals do not reconcile.";
  }
}
function geoReconcileNextActionText(nextAction) {
  switch (nextAction) {
    case "none": return "No action needed.";
    case "run_geo_sync_for_missing_dates": return "Run Google Ads Geo Sync for the missing dates.";
    case "inspect_geo_query_parity": return "Inspect geo/campaign query parity — running Geo Sync again will not close this gap. Country Period Revenue / Ad Spend stays blocked until the geo source can reconcile.";
    default: return "Inspect the geo/campaign reconciliation gap.";
  }
}
function renderGeoReconcileDetail(r, cur) {
  const money = (v) => (v === null || v === undefined ? "—" : fmtCurrency(v, cur));
  // PR-ADS-131: reconciled-with-residual is NOT a blocked root-cause — it is a safe
  // audit state. Show the split (campaign spend = country-attributed geo spend +
  // unattributed residual) and make clear country ROAS renders with a residual row.
  // Running Geo Sync again is NOT presented as the fix.
  if (r && r.country_spend_status === "reconciled_with_residual") {
    const pct = (r.country_residual_pct == null) ? "" : ` (${r.country_residual_pct}%)`;
    return `
    <div class="revenue-geo-diagnostics">
      <div class="revenue-status-label">Geo reconciliation status: Reconciled with residual</div>
      <div class="revenue-summary-strip revenue-summary-strip--three">
        <div><span>Campaign spend</span><strong>${money(r.canonical_campaign_total)}</strong></div>
        <div><span>Geo country-attributed spend</span><strong>${money(r.geo_total)}</strong></div>
        <div><span>Unknown / Unattributed country residual</span><strong>${money(r.country_residual_native)}${pct}</strong></div>
      </div>
      <p class="revenue-geo-cause">Country Period Revenue / Ad Spend is shown with a residual row. Real country rows use country-attributed spend only. Running Geo Sync again will not close this gap — the geographic view does not assign this spend to a country by design.</p>
    </div>`;
  }
  if (!r || !r.status || r.status === "reconciled" || r.status === "no_geo_data" || r.status === "unavailable") {
    return "";
  }
  const missingCount = (r.missing_geo_dates || []).length;
  const campGaps = (r.largest_campaign_gaps || []).slice(0, 5);
  const dayGaps = (r.largest_daily_gaps || []).slice(0, 5);
  const gapList = (items, label) => (items.length
    ? `<div class="revenue-status-label">${label}</div>
       <ul class="revenue-geo-gap-list">${items.map((g) => `<li>${escapeHtml(g.campaign_name || g.campaign_id || g.date || "—")}: ${money(g.variance_native)}</li>`).join("")}</ul>`
    : "");
  return `
    <div class="revenue-geo-diagnostics">
      <div class="revenue-status-label">Geo Reconciliation Root Cause</div>
      <p class="revenue-geo-cause">${escapeHtml(geoReconcileCauseText(r.reason))}</p>
      <div class="revenue-summary-strip revenue-summary-strip--three">
        <div><span>Tolerance</span><strong>${fmtTolerancePct(r.tolerance)}</strong></div>
        <div><span>Missing geo dates</span><strong>${fmtCount(missingCount)}</strong></div>
        <div><span>Unknown / unattributed spend</span><strong>${money(r.unknown_location_spend_native)}</strong></div>
      </div>
      ${gapList(campGaps, "Largest campaign gaps")}
      ${gapList(dayGaps, "Largest daily gaps")}
      <div class="revenue-status-label">Next action</div>
      <p class="revenue-geo-next">${escapeHtml(geoReconcileNextActionText(r.next_action))}</p>
    </div>`;
}

function renderGeoSyncPanel(reconcile, status) {
  const panel = document.getElementById("geo-sync-panel");
  if (!panel) return;
  const r = reconcile || {};
  const st = status || {};
  const cur = r.currency_code || "GBP";
  const running = st.running === true;
  const isAdmin = _currentUser && _currentUser.role === "admin";
  const money = (v) => (v === null || v === undefined ? "—" : fmtCurrency(v, cur));
  const variance = (r.variance === null || r.variance === undefined)
    ? "—"
    : `${r.variance > 0 ? "+" : ""}${fmtCurrency(r.variance, cur)}${r.variance_pct === null || r.variance_pct === undefined ? "" : ` (${r.variance_pct}%)`}`;
  const sm = st.summary || {};
  const rowsWritten = (sm.rows_written === null || sm.rows_written === undefined)
    ? (r.geo_rows_counted || 0) : sm.rows_written;

  const progressHtml = running
    ? `<div class="recovery-progress">Running… phase: <strong>${escapeHtml(st.phase || "starting")}</strong>${st.current_chunk ? ` · chunk ${escapeHtml(st.current_chunk)}` : ""}</div>`
    : "";
  const noticeHtml = geoSyncNotice ? `<p class="revenue-footnote">${escapeHtml(geoSyncNotice)}</p>` : "";

  const actions = isAdmin
    ? `<div class="recovery-actions">
         <button type="button" class="btn btn--secondary" data-geo-sync-action="dry-run" ${running ? "disabled" : ""}>Preview Google Ads geo sync</button>
         <button type="button" class="btn btn--primary" data-geo-sync-action="run" ${running ? "disabled" : ""}>Run Google Ads geo sync</button>
       </div>`
    : `<p class="revenue-footnote">Ask an admin to run Google Ads Geo Sync from Revenue Health.</p>`;

  panel.innerHTML = `
    <div class="panel recovery-panel">
      <div class="panel__header">Google Ads Geo Sync</div>
      <div class="panel__body">
        <p class="revenue-footnote">Country Period Revenue / Ad Spend needs geo-table spend to reconcile with canonical campaign spend. Runs in the background. Reads Google Ads read-only (geographic_view) and writes only the local database; never writes to Google Ads or HubSpot.</p>
        <div class="revenue-status-grid">
          ${geoSyncStatusBadge(r.country_spend_status === "reconciled_with_residual" ? "reconciled_with_residual" : r.status)}
        </div>
        <div class="revenue-summary-strip revenue-summary-strip--four">
          <div><span>Canonical campaign spend</span><strong>${money(r.canonical_campaign_total)}</strong></div>
          <div><span>Geo spend</span><strong>${money(r.geo_total)}</strong></div>
          <div><span>Variance</span><strong>${variance}</strong></div>
          <div><span>Latest geo sync</span><strong>${escapeHtml(r.last_geo_sync_at || "Never")}</strong></div>
        </div>
        <div class="revenue-summary-strip revenue-summary-strip--three">
          <div><span>Rows written</span><strong>${fmtCount(rowsWritten)}</strong></div>
          <div><span>Countries</span><strong>${fmtCount(r.geo_country_count)}</strong></div>
          <div><span>Coverage / FX</span><strong>${escapeHtml(r.coverage_status || "—")} / ${escapeHtml(r.fx_coverage_status || "—")}</strong></div>
        </div>
        ${renderGeoReconcileDetail(r, cur)}
        ${actions}
        ${progressHtml}
        ${noticeHtml}
      </div>
    </div>
  `;
}

function stopGeoSyncPolling() {
  if (geoSyncPollTimer) { clearTimeout(geoSyncPollTimer); geoSyncPollTimer = null; }
}

async function fetchGeoReconcile() {
  const window_ = getRoasBusinessWindow();
  try {
    const res = await fetch(`/api/google-ads-geo-reconcile?window=${encodeURIComponent(window_)}`, { credentials: "same-origin" });
    if (!res.ok) return null;
    return res.json();
  } catch (_) { return null; }
}

async function fetchGeoSyncStatus() {
  const url = geoSyncJobId
    ? `/api/google-ads-geo-sync/status?job_id=${encodeURIComponent(geoSyncJobId)}`
    : "/api/google-ads-geo-sync/status";
  try {
    const res = await fetch(url, { credentials: "same-origin" });
    if (!res.ok) return null;
    return res.json();
  } catch (_) { return null; }
}

async function pollGeoSyncOnce() {
  if (_currentPage !== "revenue-health") { stopGeoSyncPolling(); return; }
  try {
    const status = await fetchGeoSyncStatus();
    if (status) {
      if (status.job_id) geoSyncJobId = status.job_id;
      if (!status.running) geoSyncReconcile = await fetchGeoReconcile();
      renderGeoSyncPanel(geoSyncReconcile, status);
      if (status.running) {
        geoSyncPollTimer = setTimeout(pollGeoSyncOnce, GEO_SYNC_POLL_MS);
        return;
      }
    }
  } catch (err) {
    console.error("[pollGeoSync]", err);
  }
  stopGeoSyncPolling();
}

async function loadGeoSync() {
  const panel = document.getElementById("geo-sync-panel");
  if (!panel) return;
  stopGeoSyncPolling();
  geoSyncNotice = "";
  try {
    const [reconcile, status] = await Promise.all([fetchGeoReconcile(), fetchGeoSyncStatus()]);
    geoSyncReconcile = reconcile;
    if (status && status.job_id) geoSyncJobId = status.job_id;
    renderGeoSyncPanel(reconcile, status);
    if (status && status.running) {
      geoSyncPollTimer = setTimeout(pollGeoSyncOnce, GEO_SYNC_POLL_MS);
    }
  } catch (err) {
    console.error("[loadGeoSync]", err);
    renderGeoSyncPanel(null, null);
  }
}

async function runGeoSyncJob(dryRun) {
  geoSyncNotice = "";
  renderGeoSyncPanel(geoSyncReconcile, { running: true, phase: dryRun ? "dry-run" : "starting" });
  try {
    const res = await fetch("/api/google-ads-geo-sync/run", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ window: getRoasBusinessWindow(), dry_run: dryRun }),
    });
    if (res.status === 409) {
      geoSyncNotice = "A geo sync job is already running.";
      loadGeoSync();
      return;
    }
    if (res.status === 403) {
      geoSyncNotice = "Admin access is required to run the Google Ads geo sync.";
      loadGeoSync();
      return;
    }
    if (!res.ok) {
      geoSyncNotice = "Google Ads geo sync could not be started.";
      loadGeoSync();
      return;
    }
    const accepted = await res.json();
    geoSyncJobId = accepted.job_id || null;
    stopGeoSyncPolling();
    pollGeoSyncOnce();
  } catch (err) {
    console.error("[runGeoSyncJob]", err);
    geoSyncNotice = "Google Ads geo sync request failed.";
    loadGeoSync();
  }
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-geo-sync-action]");
  if (!btn) return;
  runGeoSyncJob(btn.dataset.geoSyncAction === "dry-run");
});

// ── Lead Event-Date Reconciliation panel (PR-ADS-115, admin-only) ───────────
// Resolves paid leads missing a HubSpot event date: recover from real createdate,
// exclude verifiable legacy/orphan rows (audited), or leave transient failures
// unresolved. Reads HubSpot only; never writes to HubSpot or invents dates.

let leadReconJobId = null;
let leadReconPollTimer = null;
let leadReconMissingCount = null;
let leadReconNotice = "";
const LEAD_RECON_POLL_MS = 2500;

function renderReconResult(status) {
  const s = (status && status.summary) || null;
  if (!s) return "";
  const excluded = (s.no_usable_contact_identity || 0) + (s.hubspot_contact_not_found || 0) + (s.hubspot_contact_no_createdate || 0);
  const unresolved = s.remaining_unresolved || 0;
  const mode = status.dry_run ? "Preview (no writes)" : "Reconciliation";
  const ready = !status.dry_run && status.status === "success" && unresolved === 0;
  const rows = [
    ["Missing event dates", s.missing_event_dates],
    ["Recoverable by Contact ID", s.recoverable_by_contact_id],
    ["Recovered from HubSpot", s.recovered_from_createdate],
    ["No usable contact identity", s.no_usable_contact_identity],
    ["HubSpot contact not found", s.hubspot_contact_not_found],
    ["HubSpot contact has no createdate", s.hubspot_contact_no_createdate],
    // PR-ADS-180C-F2: None when the exclusions table could not be read.
    ["Already excluded legacy rows", s.already_excluded_legacy, fmtCountOrUnknown],
    ["Remaining unresolved", unresolved],
  ];
  const errorsHtml = (status.errors && status.errors.length)
    ? `<div class="revenue-next-step"><strong>Errors:</strong><ul class="revenue-health-blockers">${status.errors.map((e) => `<li>${escapeHtml(String(e))}</li>`).join("")}</ul></div>`
    : "";
  const readyLine = status.dry_run ? "" :
    `<div class="recovery-result__head">Revenue truth lead dates: <strong>${ready ? "Ready" : "Blocked"}</strong></div>`;
  return `
    <div class="recovery-result">
      <div class="recovery-result__head">${escapeHtml(mode)} · <strong>${escapeHtml(status.status || "")}</strong></div>
      <div class="revenue-summary-strip revenue-summary-strip--four">
        ${rows.map(([label, val, fmt]) => `<div><span>${label}</span><strong>${(fmt || fmtCount)(val)}</strong></div>`).join("")}
      </div>
      ${readyLine}
      ${errorsHtml}
    </div>
  `;
}

function renderLeadReconciliationPanel(status) {
  const panel = document.getElementById("lead-reconciliation-panel");
  if (!panel) return;
  const st = status || {};
  const running = st.running === true;
  const missingLabel = (leadReconMissingCount === null || leadReconMissingCount === undefined)
    ? "—" : fmtCount(leadReconMissingCount);
  const progressHtml = running
    ? `<div class="recovery-progress">Running… phase: <strong>${escapeHtml(st.phase || "loading")}</strong>${st.current_chunk ? ` · ${escapeHtml(st.current_chunk)}` : ""}</div>`
    : "";
  const noticeHtml = leadReconNotice ? `<p class="revenue-footnote">${escapeHtml(leadReconNotice)}</p>` : "";
  panel.innerHTML = `
    <div class="panel recovery-panel">
      <div class="panel__header">Lead Event-Date Reconciliation</div>
      <div class="panel__body">
        <div class="recovery-statuses">
          <div>Included paid leads missing an event date: <strong>${missingLabel}</strong></div>
        </div>
        <div class="recovery-actions">
          <button type="button" class="btn btn--secondary" data-reconcile-action="preview" ${running ? "disabled" : ""}>Preview reconciliation</button>
          <button type="button" class="btn btn--primary" data-reconcile-action="run" ${running ? "disabled" : ""}>Run reconciliation</button>
        </div>
        <p class="revenue-footnote">This reads HubSpot only. It never writes to HubSpot or invents dates.</p>
        ${progressHtml}
        ${noticeHtml}
        ${renderReconResult(st)}
      </div>
    </div>
  `;
}

function stopLeadReconPolling() {
  if (leadReconPollTimer) { clearTimeout(leadReconPollTimer); leadReconPollTimer = null; }
}

async function fetchLeadReconStatus() {
  const url = leadReconJobId
    ? `/api/lead-reconciliation/status?job_id=${encodeURIComponent(leadReconJobId)}`
    : "/api/lead-reconciliation/status";
  const res = await fetch(url, { credentials: "same-origin" });
  if (!res.ok) return null;
  return res.json();
}

async function pollLeadReconOnce() {
  if (_currentPage !== "revenue-health") { stopLeadReconPolling(); return; }
  try {
    const status = await fetchLeadReconStatus();
    if (status) {
      if (status.job_id) leadReconJobId = status.job_id;
      renderLeadReconciliationPanel(status);
      if (status.running) {
        leadReconPollTimer = setTimeout(pollLeadReconOnce, LEAD_RECON_POLL_MS);
        return;
      }
      // Job finished: refresh the source-health driven count + health body.
      loadRevenueHealth();
    }
  } catch (err) {
    console.error("[pollLeadRecon]", err);
  }
  stopLeadReconPolling();
}

async function loadLeadReconciliationStatus(missingCount) {
  const panel = document.getElementById("lead-reconciliation-panel");
  if (!panel) return;
  if (missingCount !== undefined) leadReconMissingCount = missingCount;
  stopLeadReconPolling();
  leadReconNotice = "";
  try {
    const status = await fetchLeadReconStatus();
    if (!status) { renderLeadReconciliationPanel(null); return; }
    if (status.job_id) leadReconJobId = status.job_id;
    renderLeadReconciliationPanel(status);
    if (status.running) leadReconPollTimer = setTimeout(pollLeadReconOnce, LEAD_RECON_POLL_MS);
  } catch (err) {
    console.error("[loadLeadReconciliationStatus]", err);
    renderLeadReconciliationPanel(null);
  }
}

async function runLeadReconciliationJob(dryRun) {
  leadReconNotice = "";
  renderLeadReconciliationPanel({ running: true, phase: dryRun ? "preview" : "loading" });
  try {
    const res = await fetch("/api/lead-reconciliation/run", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: dryRun }),
    });
    if (res.status === 409) {
      leadReconNotice = "A reconciliation job is already running.";
      loadLeadReconciliationStatus();
      return;
    }
    if (!res.ok) {
      leadReconNotice = "Lead reconciliation could not be started.";
      renderLeadReconciliationPanel(null);
      return;
    }
    const accepted = await res.json();
    leadReconJobId = accepted.job_id || null;
    stopLeadReconPolling();
    pollLeadReconOnce();
  } catch (err) {
    console.error("[runLeadReconciliationJob]", err);
    leadReconNotice = "Lead reconciliation request failed.";
    renderLeadReconciliationPanel(null);
  }
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-reconcile-action]");
  if (!btn) return;
  runLeadReconciliationJob(btn.dataset.reconcileAction === "preview");
});

// ── Channels (PR-ADS-162A; redesigned in PR-ADS-181A) ─────────────────────
// The acquisition cohort per channel beside the deals that closed in the same
// days. Each half publishes or withholds on its own verdict; when one is not
// published the page shows the withholding word and its reason — never a number.

const CHANNEL_FUNNEL_REASON_LABELS = {
  canonical_funnel_unreadable: "the contact data could not be read",
  cohort_reconciliation_failed: "the channel rows do not add up to the total",
  data_watermark_unknown: "the time of the last contact sync is unknown",
  source_bootstrap_incomplete: "the contact history is still being copied from HubSpot",
  source_last_incremental_failed: "the last contact sync failed",
  source_no_successful_incremental: "no contact sync has succeeded yet",
  source_sync_state_missing: "contacts have not been synced yet",
  source_sync_state_unavailable: "the contact sync state could not be read",
  source_incremental_provenance_missing: "the last contact sync cannot be verified",
};

let _channelsReqSeq = 0;
let _channelsWindow = "180d";
let _channelsData = null;
// What the reader chose to look at. It changes the presentation only: every
// figure on the page is a count the API published, or a share of two of them.
// `sort` / `dir`: the table's order (null: the default, customers first);
// `drawer`: the key of the channel whose drawer is open (null: closed).
const _channelsView = { sort: null, dir: null, drawer: null };

// PR-ADS-162I. A rate on a handful of contacts moves by several points with
// each one. Below this many contacts a channel's rates are still shown, but
// marked, and ranked after the channels with enough contacts to compare.
const CHANNEL_FUNNEL_SMALL_SAMPLE = 30;

// A missing count is unknown, never zero: it renders as a dash with no share.
function channelFunnelCount(v) {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function channelFunnelRate(part, whole) {
  const p = channelFunnelCount(part);
  const w = channelFunnelCount(whole);
  return (p === null || !w) ? null : 100 * p / w;
}

function channelFunnelIsSmall(row) {
  const n = channelFunnelCount(row.contacts);
  return n !== null && n < CHANNEL_FUNNEL_SMALL_SAMPLE;
}

function channelFunnelSmallBadge() {
  return ` <span class="dash-badge dash-badge--muted" title="Fewer than ${CHANNEL_FUNNEL_SMALL_SAMPLE} contacts. Each contact moves these percentages by several points, so compare with care.">small sample</span>`;
}

function channelFunnelHasDrill(ch) {
  const d = ch.drilldowns || [];
  return d.length > 1 || (d.length === 1 && d[0].value !== "(none)");
}

// ── PR-ADS-168A — outcomes: the deals that CLOSED in the same days ─────────
// A second cohort beside the funnel. Every figure is the API's {value,
// status, reason}, rendered by canonicalMetricHtml / rowMetricHtml: a
// published 0 is "0", anything else is the word and the reason. No share is
// computed in an outcome cell, and nothing joins the two cohorts.
const CHANNEL_OUTCOME_UNRESOLVED = "__deal_source_unresolved__";
const CHANNEL_OUTCOME_NO_SOURCE = "(no source recorded)";
const CHANNEL_OUTCOME_CLASS_LABELS = {
  contacts_disagree: "contacts have different sources",
  no_associated_contact: "no associated contact",
  association_lookup_failed: "the contact lookup failed",
  associated_contacts_not_stored: "associated contacts are not stored",
  contact_not_in_funnel: "a contact is not in the contact data",
  contact_has_no_source: "a contact has no original source",
};
function channelOutcomeInt(v) { return Number(v).toLocaleString(); }
function channelOutcomeRatio(v) { return Number(v).toFixed(2) + "x"; }

function channelOutcomeOpts(o) {
  return { windowType: "evidence", windowKey: ((o && o.window) || {}).window_key };
}

// Rows exist only when the window's deal count is published.
function channelOutcomesPublished(o) {
  return !!(o && Array.isArray(o.channels));
}

// A published row whose deal count is a number above zero.
function channelOutcomeHasDeals(row) {
  const m = ((row || {}).metrics || {}).closed_won_deals || {};
  return m.status === "published" && typeof m.value === "number" && m.value > 0;
}

function channelOutcomeClassText(row) {
  const by = (row || {}).unresolved_by_class || {};
  const parts = Object.keys(CHANNEL_OUTCOME_CLASS_LABELS)
    .filter((k) => typeof by[k] === "number" && by[k] > 0)
    .map((k) => `${CHANNEL_OUTCOME_CLASS_LABELS[k]}: ${by[k].toLocaleString()}`);
  return parts.length ? parts.join(" · ") : "";
}

// The Google Ads line's evidence split: "7 deals: 6 with a Google click ID,
// 1 by HubSpot source only". Only published counts are shown.
function channelOutcomeSplitText(line) {
  const sp = (line || {}).evidence_split;
  if (!sp) return "";
  const n = (part) => {
    const m = ((sp[part] || {}).closed_won_deals) || {};
    return m.status === "published" && typeof m.value === "number" ? m.value : null;
  };
  const g = n("with_gclid");
  const h = n("source_classification_only");
  if (g === null || h === null) return "";
  const total = g + h;
  return `${total.toLocaleString()} ${total === 1 ? "deal" : "deals"}: ${g.toLocaleString()} with a Google click ID, ${h.toLocaleString()} by HubSpot source only`;
}

// ── PR-ADS-171B — the Meta Ads (spend) line ─────────────────────────────────
// Meta spend from Meta's own ad accounts, republished as the API decided it.
// Spend only: no HubSpot count sits beside it and nothing is divided by it.
// When the figure rests on the owner's retirement declaration the disclosure
// is shown beside it — and a figure whose disclosure is missing is not shown.
function metaLineRetired(line) {
  return Array.isArray((line || {}).owner_declared_retired) && line.owner_declared_retired.length > 0;
}

function metaLineSpendMetric(line) {
  const l = line || {};
  if (metaLineRetired(l) && !l.owner_declared_retired_disclosure) {
    return { value: null, status: "withheld", reason: "meta_retired_disclosure_missing" };
  }
  return l.spend || { value: null, status: "unavailable", reason: "meta_ads_spend_line_failed" };
}

function metaNativeMoney(v, currency) {
  return `${Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${currency || ""}`.trim();
}

const META_ACCOUNT_STATE_LABELS = {
  verified: "verified", verified_zero_spend: "verified, no spend",
  coverage_incomplete: "not verified for every day", fx_incomplete: "no USD rate for every day",
  source_unavailable: "could not be read",
};

// One line per ad account: its own currency, its own time zone and the last
// day verified in that zone. A figure only when the line's total is published.
function metaLineAccountTexts(line) {
  const l = line || {};
  const spendOk = metaLineSpendMetric(l).status === "published";
  return (l.accounts || []).map((a) => {
    const parts = [`Ad account ${a.account_id}${a.account_name ? ` (${a.account_name})` : ""}`,
      a.currency || "currency unknown",
      `${a.time_zone || "time zone unknown"} days`,
      `verified through ${a.verified_through || "no day"}`];
    const st = META_ACCOUNT_STATE_LABELS[a.state];
    if (a.state && a.state !== "verified" && a.state !== "verified_zero_spend") parts.push(st || String(a.state).replace(/_/g, " "));
    if (a.owner_declared_retired) parts.push("declared retired by the owner");
    const n = a.native_spend || {};
    if (spendOk && n.status === "published" && n.value !== null && n.value !== undefined) {
      parts.push(metaNativeMoney(n.value, n.currency || a.currency));
    }
    return parts.join(" · ");
  });
}

// Everything said beside the figure, in order: the accounts, the days each is
// read on, the owner's declaration (whenever the figure rests on it), why it
// is withheld, and that nothing on the page is divided by it.
function metaLineNotes(line) {
  const l = line || {};
  const out = metaLineAccountTexts(l);
  if (l.day_basis_disclosure) out.push(l.day_basis_disclosure);
  if (l.through_disclosure) out.push(l.through_disclosure);
  if (metaLineRetired(l) && l.owner_declared_retired_disclosure) out.push(l.owner_declared_retired_disclosure);
  if (l.withheld_detail) out.push(l.withheld_detail);
  out.push("Not matched to HubSpot's Paid Social contacts or deals — no rate is calculated from it");
  return out;
}

function metaLineNativeHtml(line) {
  const l = line || {};
  if (metaLineRetired(l) && !l.owner_declared_retired_disclosure) return "";
  const n = l.native_spend || {};
  if (metaLineSpendMetric(line).status !== "published" && n.status === "published") {
    // fx_incomplete: the native total is published while USD is not.
    return canonicalMetricHtml(n, (v) => metaNativeMoney(v, n.currency));
  }
  if (n.status !== "published") return "";
  return escapeHtml(metaNativeMoney(n.value, n.currency));
}

// PR-ADS-180A — the Channels funnel as the shared block: the acquisition
// cohort's totals, read from THIS response's `totals`, `stages` and
// `publication` alone (PR-ADS-181A: the page's "Contacts acquired" cards and
// each channel's drawer read this model — one set of stage rules). A withheld cohort is the word and its reason in every
// stage — the totals are never read. The block shows no step conversion here
// (the page publishes none); each stage's share of the contacts acquired stays
// in its sub-line, as before.
const CHANNEL_STAGE_SLOTS = {
  reached_mql: "mql", reached_sql: "sql",
  reached_opportunity: "opportunity", reached_customer_stage: "customer",
};

function channelFunnelModel(data) {
  const d = data || {};
  // The Channels page publishes its totals with its channel rows: a response
  // that carries no rows is not a published funnel (fail closed, as on 180A).
  return acquisitionFunnelModel(d.channels ? d : { ...d, totals: null }, {
    head: "Contacts acquired", headSub: "in this period",
    reasonText: (code) => CHANNEL_FUNNEL_REASON_LABELS[code]
      || String(code || "the cohort could not be published").replace(/_/g, " "),
  });
}

/**
 * PR-ADS-180B — the ACQUISITION COHORT as the shared funnel block, for every
 * page that publishes it: Channels and Overview (all sources), Campaigns and
 * Countries (Google Ads), Pipeline (all sources, or the source group its lead
 * filters select — PR-ADS-182A). `data` is Channels-shaped — `publication`,
 * `metric_family` / `window_basis`, `stages` (reached_* keys), `totals` and
 * `contacts_without_created_at` — read from ONE response, as an argument.
 *
 * Unpublished: the word and the reason in every column; the totals are never
 * read (withheld means absent). Published: each stage counts every contact of
 * the cohort that has reached it so far, and the block draws no step
 * conversion. On Channels its share of the contacts acquired is a sub-line
 * (divided in the browser, as on 180A); `shares: false` draws none (Overview,
 * Campaigns, Countries publish no rate, and those pages divide nothing). A contact with no
 * created date is in no window, so while any exist (or the response does not
 * say) every column is a lower bound — "at least N" — and no share is stated:
 * both sides of it would be bounds.
 */
function acquisitionFunnelModel(data, opts) {
  const d = data || {};
  const o = opts || {};
  const pub = d.publication || {};
  const stages = (Array.isArray(d.stages) ? d.stages : []).filter((st) => st && CHANNEL_STAGE_SLOTS[st.key]);
  const family = { key: d.metric_family, basis: d.window_basis };
  const head = { slot: "contacts", key: "contacts", label: o.head || "Contacts acquired" };
  const reasonText = o.reasonText || ((code) => String(code || "the cohort could not be published").replace(/_/g, " "));
  if (pub.status !== "published" || !d.totals) {
    return {
      family,
      verdict: { status: (!d.publication || pub.status === "unavailable") ? "unavailable" : "withheld",
        reason: reasonText(pub.reason),
        note: o.note || "No figures are shown until the contact data is complete and current." },
      stages: [head].concat(stages.map((st) => ({
        slot: CHANNEL_STAGE_SLOTS[st.key], key: st.key, label: `Reached ${st.label}` }))),
    };
  }
  const totals = d.totals;
  const missing = "the server sent no count for this stage";
  const bound = acquisitionCohortBound(d.contacts_without_created_at);
  const shareUnknown = bound ? `The cohort itself is a lower bound: ${bound}` : "";
  return {
    family,
    verdict: { status: "published" },
    stages: [{ ...head, metric: funnelCountMetric(channelFunnelCount(totals.contacts), missing),
      bound_reason: bound, sub: o.headSub || "in this period" }].concat(stages.map((st) => {
      // `shares: false` (Overview, Campaigns, Countries): no rate is drawn at
      // all — those pages publish none, and the browser computes none there.
      if (o.shares === false) {
        return {
          slot: CHANNEL_STAGE_SLOTS[st.key], key: st.key, label: `Reached ${st.label}`,
          metric: funnelCountMetric(channelFunnelCount(totals[st.key]), missing),
          bound_reason: bound, sub: "of the contacts acquired, so far",
          // PR-ADS-180B-F1 (m6): a page's note on one column (Countries: the
          // residual inside Reached SQL). Drawn only under a published verdict.
          notes: ((o.stageNotes || {})[st.key] || []).filter((n) => n && n.text),
        };
      }
      const rate = bound ? null : channelFunnelRate(totals[st.key], totals.contacts);
      return {
        slot: CHANNEL_STAGE_SLOTS[st.key], key: st.key, label: `Reached ${st.label}`,
        metric: funnelCountMetric(channelFunnelCount(totals[st.key]), missing),
        bound_reason: bound,
        sub: rate === null ? "share unknown" : `${rate.toFixed(1)}% of contacts`,
        sub_title: shareUnknown,
      };
    })),
    conversions: [],
  };
}

/**
 * PR-ADS-180B — why an acquisition cohort is a lower bound, or null. A contact
 * with no created date belongs to no window (no other date is substituted),
 * so any such contact may be missing from this one. Not stated is not zero.
 */
function acquisitionCohortBound(withoutCreatedAt) {
  const n = channelFunnelCount(withoutCreatedAt);
  if (n === null) {
    return "this response does not say how many contacts have no created date, so contacts created in this window may be missing";
  }
  if (n <= 0) return null;
  return `${fmtCount(n)} contact${n === 1 ? " has" : "s have"} no created date in HubSpot and ${n === 1 ? "is" : "are"} in no window — some may have been created in this one`;
}

/** A cohort reason as the verdict line's words (no trailing full stop). */
function cohortFunnelReasonText(code) {
  return String(cohortReasonText(code) || "the acquisition cohort is not published")
    .replace(/\.\s*$/, "");
}

/**
 * PR-ADS-180B — a Revenue Decision Mart response's cohort (Campaigns,
 * Countries) in the acquisition adapter's shape. The gate is the cohort's own
 * `campaignSqlPublication(cohort)` — the cohort is the ARGUMENT, the block of
 * the SAME response the page's cohort counts come from. Unpublished, no total
 * is read. Each column names the `totals` field it reads (`funnel_stages`); a
 * response without them shows those columns as not on this page.
 */
function martCohortFunnelData(cohort) {
  const c = cohort || {};
  const gate = campaignSqlPublication(cohort);
  const defs = (Array.isArray(c.funnel_stages) ? c.funnel_stages : []).filter((st) => st && st.key);
  const stages = defs.filter((st) => CHANNEL_STAGE_SLOTS[st.key]);
  const head = defs.find((st) => st.key === "contacts_acquired");
  const t = c.totals || {};
  const totals = gate.publish ? { contacts: head ? t[head.total] : null } : null;
  if (totals) stages.forEach((st) => { totals[st.key] = t[st.total]; });
  return {
    publication: c.sql_status ? { status: gate.publish ? "published" : gate.state,
      reason: gate.publish ? null : c.sql_reason } : null,
    metric_family: c.metric_family, window_basis: c.window_basis,
    stages: stages.map((st) => ({ key: st.key, label: st.label })),
    totals,
    contacts_without_created_at: gate.publish ? c.contacts_without_created_at : null,
  };
}

/** The Campaigns / Countries funnel panel: ONE block for the page's cohort. */
function martCohortFunnelHtml(cohort, opts) {
  const o = opts || {};
  const model = acquisitionFunnelModel(martCohortFunnelData(cohort), {
    head: "Leads · contacts acquired", headSub: o.headSub || "Google Ads-sourced",
    reasonText: cohortFunnelReasonText, shares: false, stageNotes: o.stageNotes,
  });
  const published = campaignSqlPublication(cohort).publish;
  return `
    <section class="dash-panel dash-anim mart-cohort-funnel-panel" aria-label="${escapeHtml(o.title || "Lifecycle funnel")}">
      <div class="dash-panel__header">
        <div>
          <h3 class="dash-panel__title">${escapeHtml(o.title || "Lifecycle funnel")}</h3>
          <p class="dash-panel__sub">${escapeHtml(o.sub || "")}${escapeHtml(cohortThroughSuffix(cohort))}${published ? cohortAsOfNote(cohort) : ""}</p>
        </div>
      </div>
      ${funnelBlockHtml(model, { ariaLabel: o.title || "Lifecycle funnel" })}
      <p class="lifecycle-legend">${escapeHtml(ACQUISITION_FUNNEL_LEGEND)}</p>
    </section>`;
}

// PR-ADS-180B-F1 (m5): the legend states the stage-reach rule as it is
// (`marketing_outcome_cohort_service.stage_reached`, unchanged): a contact
// counts at a stage when HubSpot records it entering that stage OR its
// current lifecycle stage is that stage or a later one. So the columns are
// not nested subsets: a contact whose stage moved back keeps the later stage
// it entered without necessarily counting at the stages before it.
/**
 * PR-ADS-180B-F1 (m6) — the Countries funnel's Reached SQL column is the
 * WHOLE Google Ads cohort (`cohort.totals.sqls`), the residual included; the
 * SQLs card sums the listed markets only. This note says how many of the
 * column are in the residual, read from the residual row of the SAME response
 * (`residual`, an argument) and only where that row's SQL count is itself
 * published (`cohortCountPublication`, the table cell's gate). Nothing is
 * subtracted: a residual that is not published is said so in words, and a
 * response with no residual row states no residual. Gated, like the funnel,
 * by `campaignSqlPublication(cohort)`: unpublished, there is no note at all.
 */
function countryFunnelResidualNote(cohort, residual) {
  if (!campaignSqlPublication(cohort).publish) return null;
  const tail = "the SQLs card above counts the listed markets only";
  if (!residual) {
    return { tone: "bound", text: `This response carries no residual row, so how many of these are outside the listed markets is not stated; ${tail}.` };
  }
  const n = residual.sqls;
  if (cohortCountPublication(cohort, residual).publish && Number.isInteger(n) && n >= 0) {
    return { text: `Includes ${fmtCount(n)} in markets not listed or with no country (the residual row); ${tail}.` };
  }
  const why = cohortReasonText(residual.sqls_reason || residual.withheld_reason);
  return { tone: "bound", text: `Includes contacts in markets not listed or with no country; their number is not published in this response${why ? ` (${String(why).replace(/\.\s*$/, "")})` : ""}. ${tail[0].toUpperCase()}${tail.slice(1)}.` };
}

const ACQUISITION_FUNNEL_LEGEND = "Each stage counts every contact of this cohort that "
  + "has reached it so far, so a recent window keeps maturing. A contact counts at a "
  + "stage when HubSpot records it entering that stage, or when its current lifecycle "
  + "stage is that stage or a later one. The columns are not nested subsets: a contact "
  + "whose stage moved back can count at a later stage without counting at an earlier "
  + "one, so a later column can exceed an earlier one. 'Customer stage' is the "
  + "HubSpot lifecycle stage, counted in contacts — never companies, never deals.";

// ── PR-ADS-181A — the Channels page: where your customers come from ─────────
// A presentation change over the same /api/channel-funnel response. It
// computes no figure the API did not publish: every count, sum and verdict is
// the payload's, and the page divides only two published counts of ONE
// population (a channel's SQLs by its own new contacts, a row's revenue by the
// window's revenue). Two populations share the page and never meet:
//   * deals CLOSED in these days (outcomes: customers, revenue) — the reader
//     contract's {value, status, reason}, rendered by canonicalMetricHtml /
//     rowMetricHtml: a published 0 is "0", anything else the word and why;
//   * contacts CREATED in these days (the acquisition cohort: new contacts,
//     SQLs) — published or withheld as ONE funnel, through the shared funnel
//     model (channelFunnelModel → acquisitionFunnelModel). Withheld means
//     absent: no count is read under an unpublished verdict.
// Each group follows its own verdict. Customers are distinct companies: the
// "All channels" figure is the window's own count, never a sum of the rows.

const CHANNELS_WINDOWS = ["30d", "60d", "180d", "all_time"];
const CHANNELS_ALL = "__all__";
const CHANNELS_UNRESOLVED_LABEL = "Deals with no single source";

// (The reader's choices live in _channelsView, declared above.)

/** The period in the URL hash (#/channels?window=<period>), or null. */
function channelsWindowFromHash(hash) {
  const q = new URLSearchParams(String(hash || "").split("?")[1] || "");
  const w = q.get("window");
  return CHANNELS_WINDOWS.includes(w) ? w : null;
}

/** The hash that keeps a period across a refresh. The default needs none. */
function channelsHashFor(windowKey) {
  return windowKey && windowKey !== "180d" && CHANNELS_WINDOWS.includes(windowKey)
    ? `#/channels?window=${windowKey}` : "#/channels";
}

// ── formats (the Overview's): $k / $M money, "26 Nov" dates, 1-decimal % ──
function channelMoney(v) { return fmtMoney(Number(v)); }

function channelPct(part, whole) {
  const r = channelFunnelRate(part, whole);
  return r === null ? null : `${r.toFixed(1)}%`;
}

const CHANNELS_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-11-26" → "26 Nov" (with the year when `withYear`). Unknown is "". */
function channelsDay(iso, withYear) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ""));
  if (!m) return "";
  const text = `${Number(m[3])} ${CHANNELS_MONTHS[Number(m[2]) - 1]}`;
  return withYear ? `${text} ${m[1]}` : text;
}

/** A window's days in words: "31 May – 25 Nov"; the years when they differ. */
function channelsRange(w) {
  const x = w || {};
  if (!x.end_date) return "dates not stated";
  if (x.is_all_time || !x.start_date) return `All time, to ${channelsDay(x.end_date, true)}`;
  const years = String(x.start_date).slice(0, 4) !== String(x.end_date).slice(0, 4);
  return `${channelsDay(x.start_date, years)} – ${channelsDay(x.end_date, years)}`;
}

/**
 * "Contacts updated 26 Nov, 06:06 UTC" — the funnel's freshness.as_of, which
 * is the contact sync's last successful incremental (not the deals' time).
 */
function channelsUpdatedText(data) {
  const asOf = String(((data || {}).freshness || {}).as_of || "");
  const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/.exec(asOf);
  return m ? `Contacts updated ${channelsDay(m[1])}, ${m[2]} UTC` : "Contacts update time not stated";
}

// ── the published inputs, per population ────────────────────────────────────
/** The funnel half, through the shared model (one set of stage rules). */
function channelsFunnelState(data) {
  const d = data || {};
  const model = channelFunnelModel(d);
  const ok = model.verdict.status === "published";
  const stage = (slot) => model.stages.find((s) => s.slot === slot) || null;
  return {
    model, ok,
    verdict: model.verdict,
    contacts: stage("contacts"),
    sql: stage("sql"),
    // One reason for every lower bound on the page: the model's.
    bound: ok ? (stage("contacts") || {}).bound_reason || null : null,
    rows: ok ? d.channels : null,
  };
}

/** The window's own outcome metric (never a sum of the rows). */
function channelOutcomeMetric(o, key) {
  const m = (((o || {}).totals || {}).metrics || {})[key];
  return m || ((o || {}).publication || {})[key]
    || { value: null, status: "unavailable", reason: "channel_outcomes_failed" };
}

/** A row's published value for an outcome metric, or null (not published). */
function channelOutcomeValue(row, key) {
  const m = (((row || {}).metrics) || {})[ROW_METRIC_KEYS[key] || key];
  return m && m.status === "published" && typeof m.value === "number" ? m.value : null;
}

/**
 * "Most from <channel> · N" — the leading channel, or null when the answer is
 * not clear. Every candidate must be published (a withheld row could be the
 * largest); the leader must beat the runner-up by more than `uncertain`, the
 * part no channel holds (deals with no single source, contacts with no source
 * or no created date) — any of it could belong to the runner-up. A tie, a
 * zero, an unpublished row or an unknown `uncertain` says nothing.
 */
function channelLeader(candidates, uncertain) {
  const list = Array.isArray(candidates) ? candidates : [];
  if (!list.length || uncertain === null || uncertain === undefined) return null;
  if (list.some((c) => typeof c.value !== "number" || !Number.isFinite(c.value))) return null;
  const sorted = list.slice().sort((a, b) => b.value - a.value);
  const top = sorted[0];
  const second = sorted.length > 1 ? sorted[1].value : 0;
  if (!(top.value > 0) || !(top.value > second + uncertain)) return null;
  return { label: top.label, value: top.value };
}

// `valueHtml` renders the figure: a lower-bound cohort count goes through the
// block's own renderer (channelCountHtml), so a leader is "at least N" too.
function channelLeaderHtml(leader, valueHtml) {
  if (!leader) return "";
  return `<div class="channels-kpi__lead">Most from <strong>${escapeHtml(leader.label)}</strong> · ${valueHtml(leader.value)}</div>`;
}

/** The outcome leader for one metric: channel rows only. */
function channelOutcomeLeader(o, key) {
  if (!channelOutcomesPublished(o)) return null;
  const rows = o.channels.filter((r) => r.source !== CHANNEL_OUTCOME_UNRESOLVED);
  const un = o.channels.find((r) => r.source === CHANNEL_OUTCOME_UNRESOLVED);
  const uncertain = un ? channelOutcomeValue(un, key) : 0;
  return channelLeader(rows.map((r) => ({ label: r.label, value: channelOutcomeValue(r, key) })), uncertain);
}

/** The funnel leader for one count: channel rows only, the cohort published. */
function channelFunnelLeader(data, st, key) {
  if (!st.ok) return null;
  const d = data || {};
  const rows = st.rows.filter((r) => r.source !== CHANNEL_OUTCOME_NO_SOURCE);
  const none = st.rows.find((r) => r.source === CHANNEL_OUTCOME_NO_SOURCE);
  const noSource = none ? channelFunnelCount(none[key]) : 0;
  const undated = channelFunnelCount(d.contacts_without_created_at);
  const uncertain = noSource === null || undated === null ? null : noSource + undated;
  return channelLeader(rows.map((r) => ({ label: r.label, value: channelFunnelCount(r[key]) })), uncertain);
}

// ── 1. the two KPI groups ──────────────────────────────────────────────────
function channelsKpiCard(label, valueHtml, sub, lead) {
  return `
      <div class="channels-kpi">
        <div class="channels-kpi__label">${escapeHtml(label)}</div>
        <div class="channels-kpi__value">${valueHtml}</div>
        <div class="channels-kpi__sub">${sub}</div>${lead || ""}
      </div>`;
}

/** Deals closed: customers won and closed-won revenue, the window's own. */
function channelOutcomeGroupHtml(o) {
  const opts = channelOutcomeOpts(o);
  const w = (o && o.window) || {};
  const customers = channelOutcomeMetric(o, "customers");
  const revenue = channelOutcomeMetric(o, "revenue_usd");
  const deals = channelOutcomeMetric(o, "closed_won_deals");
  const dealsPublished = deals.status === "published" && typeof deals.value === "number";
  const dealSub = dealsPublished
    ? `${escapeHtml(channelOutcomeInt(deals.value))} ${deals.value === 1 ? "deal" : "deals"} · USD`
    : `Closed-won deals: ${canonicalMetricHtml(deals, channelOutcomeInt, opts)}`;
  const range = o && o.window ? channelsRange(w) : "";
  return `
    <section class="channels-group dash-anim" aria-label="Deals closed" data-metric-family="${escapeHtml((o && o.metric_family) || "not_stated")}" data-window-basis="${escapeHtml((o && o.window_basis) || "not_stated")}">
      <div class="channels-group__head"><span>Deals closed</span>${range ? `<em${w.disclosure ? ` title="${escapeHtml(w.disclosure)}"` : ""}>${escapeHtml(range)}</em>` : ""}</div>
      <div class="channels-group__cards">
        ${channelsKpiCard("Customers won", canonicalMetricHtml(customers, channelOutcomeInt, opts), "distinct companies",
          channelLeaderHtml(channelOutcomeLeader(o, "customers"), (v) => escapeHtml(channelOutcomeInt(v))))}
        ${channelsKpiCard("Closed-won revenue", canonicalMetricHtml(revenue, channelMoney, opts), dealSub,
          channelLeaderHtml(channelOutcomeLeader(o, "revenue_usd"), (v) => escapeHtml(channelMoney(v))))}
      </div>
    </section>`;
}

/** Contacts acquired: new contacts and reached SQL, from the shared model. */
function channelFunnelGroupHtml(data, st) {
  const d = data || {};
  const verdict = st.verdict;
  const contacts = st.contacts || { slot: "contacts", key: "contacts" };
  const sql = st.sql || { slot: "sql", key: "reached_sql" };
  // Withheld means absent: under an unpublished verdict the cards carry the
  // word (funnelBlockValueHtml reads no figure) and the line says why.
  const value = (stage) => funnelBlockValueHtml(stage, verdict);
  const bound = st.ok && st.bound
    ? `<span class="channels-kpi__bound" title="${escapeHtml(st.bound)}">Lower bound: ${escapeHtml(st.bound)}</span>` : "";
  const sqlSub = st.ok ? escapeHtml(sql.sub || "") : "";
  return `
    <section class="channels-group dash-anim" aria-label="Contacts acquired" data-metric-family="${escapeHtml(d.metric_family || "not_stated")}" data-window-basis="${escapeHtml(d.window_basis || "not_stated")}">
      <div class="channels-group__head"><span>Contacts acquired</span>${d.window && d.window.end_date ? `<em>${escapeHtml(channelsRange(d.window))}</em>` : ""}</div>
      ${funnelBlockVerdictHtml(verdict)}
      <div class="channels-group__cards">
        ${channelsKpiCard("New contacts", value(contacts), st.ok ? "created in these days" : "",
          channelLeaderHtml(channelFunnelLeader(d, st, "contacts"), (v) => channelCountHtml(v, st.bound, "contacts")) + bound)}
        ${channelsKpiCard("Reached SQL so far", value(sql), sqlSub,
          channelLeaderHtml(channelFunnelLeader(d, st, "reached_sql"), (v) => channelCountHtml(v, st.bound, "reached_sql")))}
      </div>
    </section>`;
}

// ── 2. one ranked table ────────────────────────────────────────────────────
const CHANNEL_TABLE_COLUMNS = [
  { key: "channel", label: "Channel", half: null },
  { key: "customers", label: "Customers", half: "outcome" },
  { key: "revenue", label: "Revenue", half: "outcome" },
  { key: "share", label: "Share of revenue", half: "outcome", wide: true },
  { key: "contacts", label: "New contacts", half: "funnel", wide: true },
  { key: "sqls", label: "SQLs", half: "funnel" },
  { key: "sql_rate", label: "SQL rate", half: "funnel", wide: true },
];

const CHANNEL_TABLE_TITLES = {
  channel: "channel name", customers: "customers won", revenue: "revenue",
  share: "share of revenue", contacts: "new contacts", sqls: "SQLs", sql_rate: "SQL rate",
};

/**
 * The table's rows, from the published halves only. A funnel channel carries
 * its funnel row and (when outcomes publish) its outcome row. A channel with
 * closed deals and no contact acquired in these days is added with a measured
 * 0 (the published funnel lists every source). "No source recorded" (a funnel
 * row) and the deals with no single source (an outcome row) are not channels:
 * they are kept, pinned last, and never lead.
 */
function channelTableRows(data, st, o) {
  const outRows = channelOutcomesPublished(o) ? o.channels : null;
  const byKey = {};
  (outRows || []).forEach((r) => { byKey[r.source] = r; });
  const rows = [];
  const seen = {};
  (st.rows || []).forEach((ch, i) => {
    seen[ch.source] = true;
    rows.push({ key: ch.source, label: ch.label, order: i, f: ch,
      out: outRows ? (byKey[ch.source] || null) : null,
      kind: ch.source === CHANNEL_OUTCOME_NO_SOURCE ? "no_source" : "channel",
      small: channelFunnelIsSmall(ch) });
  });
  (outRows || []).forEach((r, i) => {
    if (r.source === CHANNEL_OUTCOME_UNRESOLVED || seen[r.source]) return;
    if (st.ok && !channelOutcomeHasDeals(r)) return;
    rows.push({ key: r.source, label: r.label, order: 1000 + i, out: r, kind: "channel",
      f: st.ok ? { source: r.source, label: r.label, contacts: 0, reached_sql: 0 } : null, small: false });
  });
  const un = outRows ? byKey[CHANNEL_OUTCOME_UNRESOLVED] : null;
  if (un) rows.push({ key: CHANNEL_OUTCOME_UNRESOLVED, label: CHANNELS_UNRESOLVED_LABEL, order: 9999,
    out: un, f: null, kind: "unresolved", small: false });
  return rows;
}

/** A row's value for a column, or null (unknown: sorted last, never as 0). */
function channelSortValue(row, key, st) {
  const f = row.f || {};
  switch (key) {
    case "channel": return row.label || "";
    case "customers": return channelOutcomeValue(row.out, "customers");
    case "revenue": case "share": return channelOutcomeValue(row.out, "revenue_usd");
    case "contacts": return row.f ? channelFunnelCount(f.contacts) : null;
    case "sqls": return row.f ? channelFunnelCount(f.reached_sql) : null;
    case "sql_rate": return row.f && !st.bound ? channelFunnelRate(f.reached_sql, f.contacts) : null;
    default: return null;
  }
}

/** The columns this response can fill: a withheld half is absent, not zero. */
function channelTableColumns(st, o) {
  const outOk = channelOutcomesPublished(o);
  return CHANNEL_TABLE_COLUMNS.filter((c) => !c.half
    || (c.half === "outcome" && outOk) || (c.half === "funnel" && st.ok));
}

/** The sort in force: the view's when its column is shown, else the default. */
function channelTableSort(view, columns) {
  const v = view || {};
  const keys = columns.map((c) => c.key);
  const key = keys.includes(v.sort) ? v.sort
    : (keys.includes("customers") ? "customers" : (keys.includes("contacts") ? "contacts" : "channel"));
  const dir = (v.sort === key && (v.dir === "asc" || v.dir === "desc")) ? v.dir
    : (key === "channel" ? "asc" : "desc");
  return { key, dir };
}

/**
 * Channel rows in sort order; the two rows that are not channels stay last.
 * Unknown values go last whatever the direction. Sorting by SQL rate ranks a
 * channel with fewer than CHANNEL_FUNNEL_SMALL_SAMPLE contacts after the ones
 * with enough to compare (PR-ADS-162I): a 5-of-9 rate never tops the list.
 */
function channelTableSorted(rows, sort, st) {
  const chans = rows.filter((r) => r.kind === "channel");
  const rest = rows.filter((r) => r.kind !== "channel");
  const sign = sort.dir === "asc" ? 1 : -1;
  chans.sort((a, b) => {
    const x = channelSortValue(a, sort.key, st);
    const y = channelSortValue(b, sort.key, st);
    // Only between two KNOWN rates: an unknown rate (a lower-bound cohort)
    // ranks nothing, so the API's order stands.
    if (sort.key === "sql_rate" && x !== null && y !== null && a.small !== b.small) return a.small ? 1 : -1;
    if (x === null && y === null) return a.order - b.order;
    if (x === null) return 1;
    if (y === null) return -1;
    if (typeof x === "string") return sign * x.localeCompare(y) || a.order - b.order;
    return sign * (x - y) || a.order - b.order;
  });
  return chans.concat(rest);
}

function channelNa(title) {
  return `<span class="channels-na" title="${escapeHtml(title)}">n/a</span>`;
}

/**
 * A cohort count off the funnel block: "—" when not sent; otherwise the
 * block's own value renderer (funnelBlockValueHtml) — the count, or "at least
 * N" with the reason under a lower bound. No local wording.
 */
function channelCountHtml(v, bound, key) {
  const n = channelFunnelCount(v);
  if (n === null) return '<span class="channels-unknown" title="the server sent no count">—</span>';
  return funnelBlockValueHtml({ key: key || "count", metric: funnelCountMetric(n, null),
    bound_reason: bound || null }, { status: "published" });
}

function channelRateHtml(f, bound) {
  if (bound) return `<span class="channels-unknown" title="Rate unknown: ${escapeHtml(bound)}">—</span>`;
  const pct = channelPct(f.reached_sql, f.contacts);
  return pct === null ? '<span class="channels-unknown" title="no rate without new contacts">—</span>' : escapeHtml(pct);
}

/** The share-of-revenue bar: a row's published revenue over the window's. */
function channelShareHtml(row, total) {
  const v = channelOutcomeValue(row.out, "revenue_usd");
  const pct = v === null || !(total > 0) ? null : 100 * v / total;
  if (pct === null) return "";
  const w = Math.max(0, Math.min(100, pct)).toFixed(1);
  return `<span class="channels-share" aria-hidden="true"><span class="channels-share__fill" style="width:${w}%"></span></span><span class="channels-share__pct">${pct.toFixed(1)}%</span>`;
}

function channelCellHtml(row, col, ctx) {
  const f = row.f || {};
  switch (col.key) {
    case "customers": case "revenue": {
      if (row.kind === "no_source") return channelNa(`These contacts' deals are counted under ${CHANNELS_UNRESOLVED_LABEL}`);
      if (!row.out) return canonicalMetricHtml({ status: "unavailable", reason: "the server sent no outcome row for this channel" }, null, ctx.oOpts);
      return col.key === "customers"
        ? rowMetricHtml(row.out, "customers", channelOutcomeInt, ctx.oOpts)
        : rowMetricHtml(row.out, "revenue_usd", channelMoney, ctx.oOpts);
    }
    case "share": return row.kind === "no_source" ? "" : channelShareHtml(row, ctx.totalRevenue);
    case "contacts": return row.kind === "unresolved" ? channelNa("not a contact source") : channelCountHtml(f.contacts, ctx.bound);
    case "sqls": return row.kind === "unresolved" ? channelNa("not a contact source") : channelCountHtml(f.reached_sql, ctx.bound);
    case "sql_rate": return row.kind === "unresolved" ? "" : channelRateHtml(f, ctx.bound);
    default: return "";
  }
}

/** The "All channels" row: the window's OWN totals — customers never summed. */
function channelTotalCellHtml(col, ctx, data, o) {
  const t = (data || {}).totals || {};
  switch (col.key) {
    case "customers": return canonicalMetricHtml(channelOutcomeMetric(o, "customers"), channelOutcomeInt, ctx.oOpts);
    case "revenue": return canonicalMetricHtml(channelOutcomeMetric(o, "revenue_usd"), channelMoney, ctx.oOpts);
    case "share": return "";
    case "contacts": return channelCountHtml(t.contacts, ctx.bound);
    case "sqls": return channelCountHtml(t.reached_sql, ctx.bound);
    case "sql_rate": return channelRateHtml(t, ctx.bound);
    default: return "";
  }
}

function channelTableHtml(data, view, st, o) {
  const columns = channelTableColumns(st, o);
  if (columns.length === 1) return "";
  const sort = channelTableSort(view, columns);
  const total = channelOutcomeMetric(o, "revenue_usd");
  const ctx = { oOpts: channelOutcomeOpts(o), bound: st.bound,
    totalRevenue: total.status === "published" && typeof total.value === "number" ? total.value : null };
  const sorted = channelTableSorted(channelTableRows(data, st, o), sort, st);
  const colClass = (c) => `${c.key === "channel" ? "" : "num "}channels-col--${c.key}${c.wide ? " channels-col--wide" : ""}`;
  const head = columns.map((c) => {
    const on = c.key === sort.key;
    return `<th class="${colClass(c)}"${on ? ` aria-sort="${sort.dir === "asc" ? "ascending" : "descending"}"` : ""}><button type="button" class="channels-sort${on ? ` is-active is-${sort.dir}` : ""}" data-channel-sort="${c.key}" title="Sort by ${escapeHtml(c.label.toLowerCase())}">${escapeHtml(c.label)}</button></th>`;
  }).join("");
  const totalRow = `<tr class="channels-row channels-row--total" data-channel-open="${CHANNELS_ALL}" tabindex="0">${columns.map((c) =>
    `<td class="${colClass(c)}">${c.key === "channel" ? "<strong>All channels</strong>" : channelTotalCellHtml(c, ctx, data, o)}</td>`).join("")}</tr>`;
  const body = sorted.map((row) => {
    const zero = row.kind !== "channel" || channelOutcomeValue(row.out, "customers") === 0;
    const cls = `channels-row${zero && columns.some((c) => c.half === "outcome") ? " channels-row--dim" : ""}${row.kind !== "channel" ? ` channels-row--${row.kind}` : ""}`;
    return `<tr class="${cls}" data-channel-open="${escapeHtml(row.key)}" tabindex="0">${columns.map((c) => {
      if (c.key !== "channel") return `<td class="${colClass(c)}">${channelCellHtml(row, c, ctx)}</td>`;
      const pill = row.kind === "channel" && row.small ? channelFunnelSmallBadge() : "";
      return `<td class="${colClass(c)}"><span class="channels-name">${escapeHtml(row.label)}</span>${pill}</td>`;
    }).join("")}</tr>`;
  }).join("");
  // The panel says what the order is: "customers won" (distinct companies).
  const title = CHANNEL_TABLE_TITLES[sort.key] || "channel name";
  const held = [];
  if (!st.ok) held.push(`New contacts, SQLs and SQL rate are ${escapeHtml((st.verdict.status === "withheld" ? "withheld" : "unavailable"))} — ${escapeHtml(st.verdict.reason || "the funnel is not published")}.`);
  if (!channelOutcomesPublished(o)) {
    const m = channelOutcomeMetric(o, "closed_won_deals");
    held.push(`Customers and revenue are ${m.status === "withheld" ? "withheld" : "unavailable"} — ${escapeHtml(canonicalReasonLabel(m.reason) || "the channel outcomes could not be loaded")}.`);
  }
  return `
    <section class="dash-panel dash-anim channels-table-panel" aria-label="Channels ranked">
      <div class="dash-panel__header"><div>
        <h3 class="dash-panel__title">Channels ranked by ${escapeHtml(title)}</h3>
        <p class="dash-panel__sub">Click any column to re-rank. Click a channel to see its full funnel and sources.</p>
        ${held.map((h) => `<p class="channels-held">${h}</p>`).join("")}
      </div></div>
      <table class="data-table channels-table">
        <thead><tr>${head}</tr></thead>
        <tbody>${totalRow}${body}</tbody>
      </table>
      ${channelLegendHtml(data, o)}
    </section>`;
}

// ── the legend line and the collapsed "How this is counted" ────────────────
function channelsExpander(id, label) {
  return `<button type="button" class="channels-link" data-channels-expand="${id}" aria-expanded="false">${escapeHtml(label)}</button>`;
}

function channelsExpandPanel(id, inner) {
  return `<div class="channels-more" data-channels-panel="${id}" hidden>${inner}</div>`;
}

function channelLegendHtml(data, o) {
  const d = data || {};
  const notes = d.notes || {};
  const on = (o && o.notes) || {};
  const customers = channelOutcomeMetric(o, "customers");
  const total = customers.status === "published" && typeof customers.value === "number"
    ? channelOutcomeInt(customers.value) : "the total";
  const w = (o && o.window) || {};
  const howto = [
    notes.channels, notes.stages, notes.maturity, notes.platforms,
    on.cohorts, on.attribution, on.original_source, on.customers,
    w.disclosure ? `Deals closed: ${w.disclosure}` : "",
    ACQUISITION_FUNNEL_LEGEND,
  ].filter(Boolean);
  return `
      <p class="channels-legend">Customers are distinct companies, and a company can appear under two channels, so the rows don't add up to ${escapeHtml(total)}. Customers and revenue count deals closed in these days, while new contacts and SQLs count people created in these days, so no rate joins the two. ${channelsExpander("howto", "How this is counted")}</p>
      ${channelsExpandPanel("howto", `<ul>${howto.map((t) => `<li>${escapeHtml(t)}</li>`).join("")}</ul>`)}`;
}

// ── 3. the paid channels block ─────────────────────────────────────────────
// The disclosure beside every period value, in short (its full text is the
// title and the line under the figures). Never named as an attribution ratio.
const PERIOD_RATIO_SHORT_DISCLOSURE = "Non-cohort operational ratio";

/** A period ratio: the value with its disclosure beside it, or the word. */
function channelPeriodRatioHtml(metric, fmt, opts) {
  const m = metric || {};
  if (m.status === "published" && m.value !== null && m.value !== undefined) {
    return `<span title="${escapeHtml(PERIOD_RATIO_DISCLOSURE)}">${escapeHtml(fmt(m.value))}</span>`
      + ` <small class="metric-reason channels-ratio-note" title="${escapeHtml(PERIOD_RATIO_DISCLOSURE)}">${PERIOD_RATIO_SHORT_DISCLOSURE}</small>`;
  }
  return canonicalMetricHtml(m, fmt, opts);
}

function channelPaidFigure(label, html) {
  return `<div class="channels-paid__figure"><div class="channels-paid__label">${escapeHtml(label)}</div><div class="channels-paid__value">${html}</div></div>`;
}

/** Google Ads: the measured line — spend beside the deals it is compared with. */
function channelGoogleCardHtml(o) {
  const opts = channelOutcomeOpts(o);
  const line = (o && o.google_ads_line) || null;
  const spend = (line && line.spend) || ((o || {}).spend || {}).google_ads
    || { status: "unavailable", reason: o ? "google_ads_usd_spend_unavailable" : "channel_outcomes_failed" };
  // No line: when the window's own figure is not published (a stale ledger,
  // an unreadable one) the card says THAT reason; only a published window
  // with no line is a missing evidence partition.
  const missing = (windowKey) => {
    const m = channelOutcomeMetric(o, windowKey);
    return m.status === "published"
      ? { status: "unavailable", reason: "google_ads_evidence_partition_unavailable" } : m;
  };
  const basis = { revenue_usd: "revenue_usd", period_revenue_per_ad_spend: "revenue_usd",
    period_spend_per_customer_won: "customers" };
  const metric = (key) => (line && (line.metrics || {})[key]) || missing(basis[key] || key);
  const customers = channelOutcomeValue(line, "customers");
  const deals = channelOutcomeValue(line, "closed_won_deals");
  const count = customers !== null && deals !== null
    ? `${channelOutcomeInt(customers)} ${customers === 1 ? "customer" : "customers"} from ${channelOutcomeInt(deals)} ${deals === 1 ? "deal" : "deals"} with a Google click ID or HubSpot Paid Search. ` : "";
  const on = (o && o.notes) || {};
  const matched = [channelOutcomeSplitText(line), on.spend].filter(Boolean);
  return `
      <div class="channels-paid__card channels-paid__card--google">
        <div class="channels-paid__head"><h4>Google Ads</h4>${matched.length ? channelsExpander("gads", "How deals are matched") : ""}</div>
        <div class="channels-paid__figures">
          ${channelPaidFigure("Spend", canonicalMetricHtml(spend, channelMoney, opts))}
          ${channelPaidFigure("Revenue", canonicalMetricHtml(metric("revenue_usd"), channelMoney, opts))}
          ${channelPaidFigure(PERIOD_RATIO_LABEL, channelPeriodRatioHtml(metric("period_revenue_per_ad_spend"), channelOutcomeRatio, opts))}
          ${channelPaidFigure(PERIOD_SPEND_PER_CUSTOMER_LABEL, channelPeriodRatioHtml(metric("period_spend_per_customer_won"), channelMoney, opts))}
        </div>
        <p class="channels-paid__note">${escapeHtml(count)}Deals closed in these days may come from earlier ads.</p>
        ${matched.length ? channelsExpandPanel("gads", matched.map((t) => `<p>${escapeHtml(t)}</p>`).join("")) : ""}
      </div>`;
}

// The Meta line's spend: USD as the API decided it, through the one gate
// (metaLineSpendMetric). Its own currency's total beside it only when it says
// something the USD figure does not (another currency, or USD unpublished).
function channelMetaSpendCellHtml(line, opts) {
  const usd = canonicalMetricHtml(metaLineSpendMetric(line), channelMoney, opts);
  const n = (line || {}).native_spend || {};
  const native = metaLineNativeHtml(line);
  const usdPublished = metaLineSpendMetric(line).status === "published";
  return native && (!usdPublished || (n.currency && n.currency !== "USD"))
    ? `${usd} <small class="metric-reason">${native}</small>` : usd;
}

/** Meta Ads: spend only. Nothing on the page is divided by it. */
function channelMetaCardHtml(o) {
  const opts = channelOutcomeOpts(o);
  const line = (o && o.meta_ads_line) || null;
  const spend = line ? channelMetaSpendCellHtml(line, opts)
    : canonicalMetricHtml(metaLineSpendMetric(null), channelMoney, opts);
  // The owner's retirement declaration stays BESIDE the figure it carries.
  const retired = line && metaLineRetired(line) && line.owner_declared_retired_disclosure
    ? `<p class="channels-paid__note channels-paid__note--disclosure">${escapeHtml(line.owner_declared_retired_disclosure)}</p>` : "";
  const notes = line ? metaLineNotes(line).filter((t) => !retired || t !== line.owner_declared_retired_disclosure) : [];
  return `
      <div class="channels-paid__card channels-paid__card--meta">
        <div class="channels-paid__head"><h4>Meta Ads</h4>${notes.length ? channelsExpander("meta", "Account details") : ""}</div>
        <div class="channels-paid__figures channels-paid__figures--one">${channelPaidFigure("Spend", spend)}</div>
        ${retired}
        <p class="channels-paid__note">Not yet matched to HubSpot deals, so there is no revenue ratio. Shown for spend tracking only.</p>
        ${notes.length ? channelsExpandPanel("meta", `<ul>${notes.map((t) => `<li>${escapeHtml(t)}</li>`).join("")}</ul>`) : ""}
      </div>`;
}

function channelPaidBlockHtml(o) {
  return `
    <section class="dash-panel dash-anim channels-paid" aria-label="Paid channels">
      <div class="dash-panel__header"><div>
        <h3 class="dash-panel__title">Paid channels</h3>
        <p class="dash-panel__sub">Spend in the same days, with what we can honestly set against it.</p>
      </div></div>
      <div class="channels-paid__grid">${channelGoogleCardHtml(o)}${channelMetaCardHtml(o)}</div>
    </section>`;
}

/**
 * The page. `data` is ONE /api/channel-funnel response and `view` the
 * reader's sort — both arguments; nothing here reads page state.
 */
function renderChannelFunnel(data, view) {
  const d = data || {};
  const st = channelsFunnelState(d);
  const o = d.outcomes || null;
  return `
    <div class="channels-groups">
      ${channelOutcomeGroupHtml(o)}
      ${channelFunnelGroupHtml(d, st)}
    </div>
    ${channelTableHtml(d, view, st, o)}
    ${channelPaidBlockHtml(o)}`;
}

// ── 4. the channel drawer ──────────────────────────────────────────────────
/** One channel's row of the funnel response (null: not a funnel row). */
function channelFunnelRow(data, key) {
  const d = data || {};
  if (key === CHANNELS_ALL) return d.totals || null;
  return (Array.isArray(d.channels) ? d.channels : []).find((c) => c.source === key) || null;
}

// HubSpot's drill-down values as stored, largest first as the API sent them.
// "(none)" is a contact with no detail recorded: named so, never dropped.
function channelDrawerSourcesHtml(ch, st) {
  if (!st.ok) return "";
  const drills = channelFunnelHasDrill(ch) ? ch.drilldowns.slice(0, 6) : [];
  if (!drills.length) return "";
  const name = (v) => (v === "(none)" ? "No detail recorded" : v);
  return `
      <h4 class="channels-drawer__h">Top sources (HubSpot detail)</h4>
      <ul class="channels-drawer__list">${drills.map((dd) =>
        `<li><span>${escapeHtml(name(dd.value))}</span><strong>${channelCountHtml(dd.contacts, st.bound)}</strong></li>`).join("")}</ul>`;
}

function channelDrawerPlatformsHtml(ch, st) {
  const split = st.ok ? (ch || {}).platform_split : null;
  if (!Array.isArray(split) || !split.length) return "";
  return `
      <h4 class="channels-drawer__h">By platform (HubSpot drill-down)</h4>
      <ul class="channels-drawer__list">${split.map((p) => {
        const vals = (p.drilldown_values || []).join(", ");
        const sub = p.platform === "unknown" ? "no drill-down recorded — never counted as Meta"
          : (vals ? `drill-down: ${vals}` : "");
        return `<li><span>${escapeHtml(p.label)}${sub ? `<small>${escapeHtml(sub)}</small>` : ""}</span><strong>${channelCountHtml(p.contacts, st.bound)}</strong></li>`;
      }).join("")}</ul>
      <p class="channels-drawer__note">New contacts by platform, from HubSpot. Not Meta's own figures, and no spend is divided by them.</p>`;
}

/**
 * A row's deals closed in these days — its own closed-won deals, customers
 * and revenue, each the reader contract's {value, status, reason}. All
 * channels: the window's own figures (customers never a sum of the rows).
 */
function channelDrawerOutcomesHtml(o, key) {
  const opts = channelOutcomeOpts(o);
  let cells;
  if (key === CHANNELS_ALL) {
    cells = [canonicalMetricHtml(channelOutcomeMetric(o, "closed_won_deals"), channelOutcomeInt, opts),
      canonicalMetricHtml(channelOutcomeMetric(o, "customers"), channelOutcomeInt, opts),
      canonicalMetricHtml(channelOutcomeMetric(o, "revenue_usd"), channelMoney, opts)];
  } else if (key === CHANNEL_OUTCOME_NO_SOURCE) {
    return `<p class="channels-drawer__note">Deals whose contacts have no source are counted under ${escapeHtml(CHANNELS_UNRESOLVED_LABEL)}.</p>`;
  } else {
    const row = channelOutcomesPublished(o) ? o.channels.find((r) => r.source === key) : null;
    if (!row) {
      const m = channelOutcomeMetric(o, "closed_won_deals");
      const gap = channelOutcomesPublished(o)
        ? canonicalMetricHtml({ status: "unavailable", reason: "the server sent no outcome row for this channel" }, null, opts)
        : canonicalMetricHtml(m.status === "published" ? { status: "unavailable", reason: "channel_outcomes_failed" } : m, null, opts);
      cells = [gap, gap, gap];
    } else {
      cells = [rowMetricHtml(row, "closed_won_deals", channelOutcomeInt, opts),
        rowMetricHtml(row, "customers", channelOutcomeInt, opts),
        rowMetricHtml(row, "revenue_usd", channelMoney, opts)];
    }
  }
  return `
      <h4 class="channels-drawer__h">Deals closed ${escapeHtml(channelsRange((o || {}).window))}</h4>
      <ul class="channels-drawer__list">
        <li><span>Closed-won deals</span><strong>${cells[0]}</strong></li>
        <li><span>Customers won</span><strong>${cells[1]}</strong></li>
        <li><span>Revenue</span><strong>${cells[2]}</strong></li>
      </ul>`;
}

/**
 * The drawer for one row (`key`: a source, CHANNELS_ALL or the unresolved
 * deals). The funnel is the shared block in its vertical layout, built from
 * THIS response's row by the shared model — the bound and withheld rules are
 * the block's own.
 */
function channelDrawerHtml(data, key) {
  const d = data || {};
  const o = d.outcomes || null;
  const st = channelsFunnelState(d);
  const links = `
      <p class="channels-drawer__links"><a href="#/campaigns">Open in Campaigns →</a> <a href="#/pipeline">Open in Pipeline →</a></p>`;
  const close = '<button type="button" class="channels-drawer__close" data-channels-drawer-close aria-label="Close">×</button>';
  if (key === CHANNEL_OUTCOME_UNRESOLVED) {
    const un = channelOutcomesPublished(o) ? o.channels.find((r) => r.source === CHANNEL_OUTCOME_UNRESOLVED) : null;
    const opts = channelOutcomeOpts(o);
    const why = un ? channelOutcomeClassText(un) : "";
    return `
    <div class="channels-drawer__head"><h3 id="channels-drawer-title">${escapeHtml(CHANNELS_UNRESOLVED_LABEL)}</h3>${close}</div>
    <p class="channels-drawer__sub">Deals closed ${escapeHtml(channelsRange((o || {}).window))} · not a contact source</p>
    <ul class="channels-drawer__list">
      <li><span>Closed-won deals</span><strong>${rowMetricHtml(un, "closed_won_deals", channelOutcomeInt, opts)}</strong></li>
      <li><span>Customers won</span><strong>${rowMetricHtml(un, "customers", channelOutcomeInt, opts)}</strong></li>
      <li><span>Revenue</span><strong>${rowMetricHtml(un, "revenue_usd", channelMoney, opts)}</strong></li>
    </ul>
    ${why ? `<p class="channels-drawer__note">${escapeHtml(why)}</p>` : ""}
    ${((o || {}).notes || {}).attribution ? `<p class="channels-drawer__note">${escapeHtml(o.notes.attribution)}</p>` : ""}
    ${links}`;
  }
  const row = channelFunnelRow(d, key);
  const label = key === CHANNELS_ALL ? "All channels"
    : (row ? row.label : (((o || {}).channels || []).find((r) => r.source === key) || {}).label || String(key));
  // A channel with deals and no contact in these days has no funnel row: its
  // funnel is a measured zero only because the published funnel lists every
  // source with a contact.
  const funnelData = key === CHANNELS_ALL ? d
    : { ...d, totals: st.ok ? (row || { contacts: 0, reached_mql: 0, reached_sql: 0,
      reached_opportunity: 0, reached_customer_stage: 0 }) : null };
  const block = funnelBlockHtml(channelFunnelModel(funnelData), { ariaLabel: `${label} funnel`, layout: "vertical" });
  return `
    <div class="channels-drawer__head"><h3 id="channels-drawer-title">${escapeHtml(label)}</h3>${close}</div>
    <p class="channels-drawer__sub">Contacts created ${escapeHtml(channelsRange(d.window))}</p>
    ${block}
    ${channelDrawerOutcomesHtml(o, key)}
    ${row && key !== CHANNELS_ALL ? channelDrawerSourcesHtml(row, st) + channelDrawerPlatformsHtml(row, st) : ""}
    ${links}`;
}

function renderChannelsFromCache() {
  const body = document.getElementById("channels-body");
  if (body && _channelsData) body.innerHTML = renderChannelFunnel(_channelsData, _channelsView);
  renderChannelDrawer();
}

/** Opens, refreshes or closes the drawer from the view (the one writer). */
function renderChannelDrawer() {
  const root = document.getElementById("channels-drawer-root");
  if (!root) return;
  const key = _channelsView.drawer;
  if (!key || !_channelsData) {
    root.innerHTML = "";
    root.hidden = true;
    return;
  }
  root.hidden = false;
  root.innerHTML = `<div class="channels-drawer-overlay" data-channels-drawer-close></div>
    <aside class="channels-drawer" role="dialog" aria-modal="true" aria-labelledby="channels-drawer-title">${channelDrawerHtml(_channelsData, key)}</aside>`;
  const btn = root.querySelector(".channels-drawer__close");
  if (btn) btn.focus();
}

/** Whether `key` is a row of THIS response (the drawer's channel survives a reload). */
function channelDrawerKeyExists(data, key) {
  const d = data || {};
  if (key === CHANNELS_ALL) return true;
  const inFunnel = Array.isArray(d.channels) && d.channels.some((c) => c.source === key);
  const o = d.outcomes || null;
  const inOutcomes = channelOutcomesPublished(o) && o.channels.some((r) => r.source === key);
  return inFunnel || inOutcomes;
}

function closeChannelDrawer() {
  if (!_channelsView.drawer) return;
  _channelsView.drawer = null;
  renderChannelDrawer();
}

async function loadChannels() {
  const body = document.getElementById("channels-body");
  // PR-ADS-181A: the period lives in the hash, so a refresh holds it.
  const window_ = channelsWindowFromHash(window.location.hash) || _channelsWindow || "180d";
  _channelsWindow = window_;
  const token = ++_channelsReqSeq;
  // An open drawer is dimmed until the new response arrives, then redrawn
  // from it (or closed): it never shows one period beside another's table.
  const drawerRoot = document.getElementById("channels-drawer-root");
  if (drawerRoot && _channelsView.drawer) drawerRoot.classList.add("is-refreshing");
  document.querySelectorAll("[data-channels-window]").forEach((btn) => {
    const on = btn.dataset.channelsWindow === window_;
    btn.classList.toggle("is-active", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  });
  const updated = document.getElementById("channels-updated");
  if (body) {
    if (_channelsData) body.classList.add("is-refreshing");
    else body.innerHTML = '<p class="empty-state" style="padding:var(--space-5)">Loading channels…</p>';
  }
  try {
    const data = await fetchJSON(`/api/channel-funnel?window=${encodeURIComponent(window_)}`);
    if (token !== _channelsReqSeq) return;  // a newer request superseded this one
    _channelsData = data;
    if (updated) { updated.textContent = channelsUpdatedText(data); updated.hidden = false; }
    if (body) { body.classList.remove("is-refreshing"); body.innerHTML = renderChannelFunnel(data, _channelsView); }
    if (_channelsView.drawer && !channelDrawerKeyExists(data, _channelsView.drawer)) _channelsView.drawer = null;
    if (drawerRoot) drawerRoot.classList.remove("is-refreshing");
    renderChannelDrawer();
  } catch (err) {
    console.error("[loadChannels]", err);
    if (token !== _channelsReqSeq) return;
    // A failed load never leaves the previous window's figures on screen.
    _channelsData = null;
    _channelsView.drawer = null;
    if (drawerRoot) drawerRoot.classList.remove("is-refreshing");
    renderChannelDrawer();
    if (updated) updated.hidden = true;
    if (body) {
      body.classList.remove("is-refreshing");
      body.innerHTML = '<div class="panel"><p class="empty-state" style="padding:var(--space-5)"><strong>Unavailable</strong> — the channel funnel could not be loaded.</p></div>';
    }
  }
}

// ── System: the state of the platform in plain words (PR-ADS-162C) ──────────
// It shows recorded facts and decides nothing. The certification verdict is the
// one the daily job stored; a verdict that is missing or old is "not checked",
// never a pass. Every unknown state renders as a warning, never as healthy.

// The System page is admin-only: navigate() sends anyone else to the Dashboard.
function adminOnlyDenied(page) {
  return page === "system" && !(_currentUser && _currentUser.role === "admin");
}

// ── Menu (PR-ADS-174A) ──────────────────────────────────────────────────────

// The pages of a menu item this viewer may open, in menu order. A page's tab
// hidden by showApp() (the admin-only pages) is not one of them.
function navVisiblePages(item) {
  return item.pages.filter((p) => {
    const tab = document.querySelector(`.section-tab[data-page="${p}"]`);
    return !(tab && tab.hidden);
  });
}

function applyNavGating() {
  NAV_ITEMS.forEach((item) => {
    const link = document.querySelector(`.nav-item[data-nav-item="${item.id}"]`);
    if (!link) return;
    const visible = navVisiblePages(item);
    link.dataset.page = visible.length ? visible[0] : item.pages[0];
    const li = link.closest("li");
    if (li) li.hidden = visible.length === 0;
  });
  if (_currentPage) renderNavActive(_currentPage);
}

function renderNavActive(page) {
  const itemId = navItemForPage(page);
  document.querySelectorAll(".nav-item[data-nav-item]").forEach((el) => {
    const on = el.dataset.navItem === itemId;
    el.classList.toggle("active", on);
    if (on) el.setAttribute("aria-current", "page");
    else el.removeAttribute("aria-current");
  });
  let stripShown = false;
  document.querySelectorAll(".section-tabs[data-section]").forEach((strip) => {
    const item = NAV_ITEMS.find((it) => it.id === strip.dataset.section);
    const show = !!item && item.id === itemId && navVisiblePages(item).length > 1;
    strip.hidden = !show;
    if (show) stripShown = true;
  });
  document.querySelectorAll(".section-tab[data-page]").forEach((tab) => {
    const on = tab.dataset.page === page;
    tab.classList.toggle("is-active", on);
    if (on) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  });
  const bar = document.getElementById("section-tabs-bar");
  if (bar) bar.hidden = !stripShown;
}

// What each daily check proves, in plain words — and no more than it proves.
const SYSTEM_CHECK_LABELS = {
  reader_cutover: "Every page reads won deals, customers and revenue from one source",
  closed_won_truth: "Won-deal, customer and revenue figures match the stored deal records",
  deal_company_associations: "A customer count is shown only where every won deal's company is proven",
  crm_truth_exceptions: "Every hidden customer or revenue figure names what blocks it",
  marketing_outcome_cohorts: "Lead and SQL counts on the Campaigns page add up",
  campaign_evidence: "The Campaigns page shows only figures it can prove",
};

const SYSTEM_CERT_TEXT = {
  certified: { tone: "ok", headline: "Yes — the latest daily check passed",
    detail: "The checks found no won-deal, customer or revenue figure, and no Campaigns-page lead or SQL count, that disagrees with the stored records. Figures that cannot be proven are hidden, not guessed." },
  violation: { tone: "bad", headline: "No — a daily check failed",
    detail: "A check found a figure that the stored records do not support. Treat the dashboard with caution until this is fixed." },
  not_certified: { tone: "warn", headline: "Not confirmed — a check could not finish",
    detail: "Not every check finished, so the numbers are unverified." },
  not_checked: { tone: "warn", headline: "Not checked in the last day",
    detail: "The daily check has not recorded a recent result, so the numbers are unverified." },
  unavailable: { tone: "warn", headline: "The check result could not be read",
    detail: "The database did not return the daily check result, so the numbers are unverified." },
  // PR-ADS-169A — a run in progress is not a pass and not yesterday's verdict.
  running: { tone: "warn", headline: "Not confirmed yet — the daily check is running",
    detail: "Today's check has started and has not finished, so the numbers are unverified until it does." },
};

// Every reason a won deal can block a figure: [who fixes it, one deal, several].
// "hubspot" is a record the sales team corrects; "platform" is our data sync and
// needs nothing done in HubSpot. A reason missing here is shown as "other",
// never under the HubSpot heading.
const SYSTEM_FIX_TEXT = {
  unresolved_no_company: ["hubspot", "won deal is not linked to any company", "won deals are not linked to any company"],
  ambiguous_multiple_companies: ["hubspot", "won deal is linked to more than one company", "won deals are linked to more than one company"],
  no_amount: ["hubspot", "won deal has no amount", "won deals have no amount"],
  unknown_currency: ["hubspot", "won deal has no proven currency", "won deals have no proven currency"],
  no_close_date_for_fx: ["hubspot", "won deal has no close date, so its amount cannot be converted to US dollars", "won deals have no close date, so their amounts cannot be converted to US dollars"],
  negative_amount: ["hubspot", "won deal has a negative amount", "won deals have a negative amount"],
  invalid_amount: ["hubspot", "won deal has an amount that is not a number", "won deals have an amount that is not a number"],
  missing_close_date: ["hubspot", "won deal has no close date", "won deals have no close date"],
  won_flag_true_in_another_stage: ["hubspot", "deal is marked won but sits in another stage", "deals are marked won but sit in another stage"],
  won_stage_but_won_flag_not_true: ["hubspot", "deal sits in the won stage but is not marked won", "deals sit in the won stage but are not marked won"],
  association_unavailable: ["platform", "won deal's company has not been read from HubSpot yet", "won deals' companies have not been read from HubSpot yet"],
  latest_association_attempt_failed: ["platform", "won deal's company could not be re-read on the last attempt", "won deals' companies could not be re-read on the last attempt"],
  deal_modified_after_association_observation: ["platform", "won deal changed after its company was last read", "won deals changed after their companies were last read"],
  not_certified_by_latest_complete_scan: ["platform", "won deal was not covered by the last full company check", "won deals were not covered by the last full company check"],
  home_currency_unverified: ["platform", "won deal cannot be converted to US dollars because the account's home currency is not verified", "won deals cannot be converted to US dollars because the account's home currency is not verified"],
  no_fx_rate_for_close_date: ["platform", "won deal has no exchange rate for its close date", "won deals have no exchange rate for their close dates"],
  currency_reason_not_recorded: ["platform", "won deal's US-dollar value is not proven and no reason was recorded", "won deals' US-dollar values are not proven and no reason was recorded"],
};

const SYSTEM_WINDOW_LABELS = {
  "evidence/7d": "last 7 days", "evidence/14d": "last 14 days", "evidence/30d": "last 30 days",
  "evidence/60d": "last 60 days", "evidence/180d": "last 180 days", "evidence/all_time": "all time",
  "business/current_quarter": "this quarter", "business/last_quarter": "last quarter",
  "business/last_6_months": "last 6 months", "business/ytd": "year to date", "business/all_time": "all time",
};

const SYSTEM_CONTACT_REASONS = {
  source_bootstrap_incomplete: "History is still being copied from HubSpot",
  source_last_incremental_failed: "The last update failed",
  source_no_successful_incremental: "No successful update has run yet",
  source_sync_state_missing: "No update has run yet",
  source_incremental_provenance_missing: "The last update cannot be confirmed",
};

function systemAgo(hours) {
  const h = Number(hours);
  if (hours === null || hours === undefined || !Number.isFinite(h) || h < 0) return "at an unknown time";
  if (h < 1) return "less than an hour ago";
  if (h < 48) return `${Math.round(h)} hour${Math.round(h) === 1 ? "" : "s"} ago`;
  return `${Math.round(h / 24)} days ago`;
}

function systemLimit(hours) {
  const h = Number(hours);
  return (hours !== null && hours !== undefined && Number.isFinite(h) && h > 0)
    ? `no successful update in the last ${Math.round(h)} hours` : "the last successful update is too old";
}

function systemCertCard(cert) {
  const c = cert || {};
  const text = SYSTEM_CERT_TEXT[c.state] || SYSTEM_CERT_TEXT.unavailable;
  const known = Object.prototype.hasOwnProperty.call(SYSTEM_CERT_TEXT, c.state) && c.state !== "unavailable";
  let detail = text.detail;
  if (c.state === "not_certified" && c.could_not_run) detail = "The daily check could not run, so nothing was checked.";
  if (c.state === "not_certified" && c.interrupted) detail = "The daily check stopped reporting progress before it finished (presumed interrupted), so nothing was checked.";
  let when = "";
  if (c.state === "running") {
    when = c.checked_at
      ? `Started ${systemAgo(c.age_hours)} (${escapeHtml(String(c.checked_at).slice(0, 16).replace("T", " "))} UTC).`
      : "";
  } else if (known) {
    when = c.checked_at
      ? `Last checked ${systemAgo(c.age_hours)} (${escapeHtml(String(c.checked_at).slice(0, 16).replace("T", " "))} UTC).`
      : "No check has been recorded yet.";
  }
  const audits = Array.isArray(c.audits) ? c.audits : [];
  // A pass lists what was checked; anything else lists only what did not pass.
  const shown = c.state === "certified" ? audits : audits.filter((a) => a.outcome !== "holds");
  const list = shown.map((a) => {
    const word = a.outcome === "holds" ? "Passed" : (a.outcome === "violation" ? "Failed" : "Could not finish");
    const label = SYSTEM_CHECK_LABELS[a.key] || String(a.key || "check").replace(/_/g, " ");
    return `<li class="system-check system-check--${a.outcome === "holds" ? "ok" : "bad"}">${word} — ${escapeHtml(label)}</li>`;
  }).join("");
  return `<div class="panel system-card system-card--${text.tone}">
    <div class="panel__header">Can you trust the numbers?</div>
    <div class="system-card__body">
      <p class="system-card__headline">${escapeHtml(text.headline)}</p>
      <p>${escapeHtml(detail)} ${when}</p>
      ${list ? `<ul class="system-checks">${list}</ul>` : ""}
      <p class="system-card__note">This check runs every morning. It covers won deals, customers, revenue and the Campaigns page's lead and SQL counts. It does not re-check ad spend, the Channels page's contact counts, the Pipeline page's lead counts or search terms, and it cannot see a deal or contact that was never entered in HubSpot.</p>
    </div></div>`;
}

// One line per source. Only status "current" is healthy; every other status,
// including one this code has never seen, is a warning. The one exception is
// keyed: Meta Ads with status "not_connected" (never set up) is neutral.
function systemSourceLine(src) {
  const s = src || {};
  const label = escapeHtml(s.label || String(s.key || "source").replace(/_/g, " "));
  const hidden = " Won-deal, customer and revenue figures are hidden until it does.";
  let ok = false;
  let state = "Could not be read";
  if (s.key === "meta_ads_spend" && s.status === "not_connected") {
    const kept = (Array.isArray(s.accounts) ? s.accounts : []).filter((a) => a && a.summary).map((a) =>
      `<div class="system-source__detail">${escapeHtml(String(a.summary))}</div>`).join("");
    return `<tr><td><strong>${label}</strong></td><td>—</td><td class="system-source--neutral">Not connected${kept}</td></tr>`;
  }
  if (s.key === "hubspot_contacts") {
    if (s.status === "current") { ok = true; state = "Up to date"; }
    else if (s.status === "late") state = `Late — ${systemLimit(s.limit_hours)}`;
    else if (s.status === "not_proven") state = SYSTEM_CONTACT_REASONS[s.reason] || "Not confirmed up to date";
  } else if (s.key === "hubspot_deals") {
    if (s.status === "current") { ok = true; state = "Up to date"; }
    else if (s.status === "late") state = `Late — ${systemLimit(s.limit_hours)}.${hidden}`;
    else if (s.status === "not_proven") {
      state = (s.history_copied === false ? "History is still being copied from HubSpot."
        : "Not confirmed — the last update did not succeed.") + hidden;
    }
  } else if (s.key === "google_ads_spend") {
    if (s.status === "current" && s.complete_through) { ok = true; state = `Complete through ${escapeHtml(String(s.complete_through))}`; }
    else if (s.status === "late") {
      const ranges = (Array.isArray(s.missing_ranges) ? s.missing_ranges : []).map((r) =>
        r.start === r.end ? String(r.start) : `${r.start} to ${r.end}`);
      state = ranges.length ? `Late — no spend recorded for ${escapeHtml(ranges.join(", "))}`
        : "Late — a recent spend update failed";
    }
  } else if (s.key === "meta_ads_spend") {
    if (s.status === "current" && s.complete_through) { ok = true; state = `Complete through ${escapeHtml(String(s.complete_through))}`; }
    else if (s.status === "misconfigured") state = `Set up incorrectly — check the Meta settings${s.reason_detail ? ` (${escapeHtml(String(s.reason_detail))})` : ""}`;
    else if (s.status === "token_rejected") state = "Meta rejected the access token — it must be renewed";
    else if (s.status === "not_readable") state = "An ad account is not readable — its history is kept";
    else if (s.status === "account_unresolved") state = "An ad account is neither connected nor declared retired — later periods are withheld";
    else if (s.status === "usd_unavailable") state = "Spend is recorded, but its US-dollar value is not available";
    else if (s.reason === "meta_api_version_deprecated") {
      state = `Meta no longer accepts the API version in use${s.reason_detail ? ` — ${escapeHtml(String(s.reason_detail))}` : ""}`;
    }
    else if (s.status === "not_proven") state = "Connected — no successful update yet";
    else if (s.status === "late") {
      const ranges = (Array.isArray(s.missing_ranges) ? s.missing_ranges : []).map((r) =>
        r.start === r.end ? String(r.start) : `${r.start} to ${r.end}`);
      state = ranges.length ? `Late — no spend recorded for ${escapeHtml(ranges.join(", "))}`
        : "Late — a recent spend update failed";
    }
    // Each ad account in its own words (its own dates, in its own time zone).
    const accounts = (Array.isArray(s.accounts) ? s.accounts : []).filter((a) => a && a.summary);
    if (accounts.length > 1 || accounts.some((a) => a.status !== "current")) {
      state += accounts.map((a) =>
        `<div class="system-source__detail">${escapeHtml(String(a.summary))}</div>`).join("");
    }
  }
  const when = s.last_success_at ? `Last successful update ${systemAgo(s.age_hours)}` : "No successful update on record";
  return `<tr><td><strong>${label}</strong></td><td>${when}</td><td class="system-source--${ok ? "ok" : "warn"}">${state}</td></tr>`;
}

function systemFixCard(cert) {
  const c = cert || {};
  const fix = c.crm_data_to_fix;
  const header = `<div class="panel__header">What needs fixing</div>`;
  if (!fix || typeof fix !== "object") {
    return `<div class="panel system-card">${header}
      <div class="system-card__body"><p><strong>Unknown</strong> — the daily check did not record this.</p></div></div>`;
  }
  const groups = { hubspot: [], platform: [], other: [] };
  Object.values(fix.by_family || {}).forEach((fam) => {
    Object.entries((fam || {}).by_reason || {}).forEach(([reason, n]) => {
      const t = SYSTEM_FIX_TEXT[reason];
      const count = Number.isFinite(Number(n)) && n !== null ? Number(n) : null;
      const phrase = t ? (count === 1 ? t[1] : t[2]) : `won deal(s): ${String(reason).replace(/_/g, " ")}`;
      groups[t ? t[0] : "other"].push(
        `<li><strong>${count === null ? "An unknown number of" : count.toLocaleString()}</strong> ${escapeHtml(phrase)}.</li>`);
    });
  });
  const blockers = (Array.isArray(fix.dataset_blockers) ? fix.dataset_blockers : []).map((b) => {
    const o = (b && typeof b === "object") ? b : { code: b };
    const words = o.explanation || String(o.code || "unnamed problem").replace(/_/g, " ");
    return `<li>${escapeHtml(String(words))}</li>`;
  });
  const windows = Array.isArray(fix.windows) ? fix.windows : [];
  const hidden = { customers: [], revenue_usd: [] };
  windows.forEach((w) => {
    const name = SYSTEM_WINDOW_LABELS[w.window] || String(w.window);
    ["customers", "revenue_usd"].forEach((k) => {
      if (w[k] !== "published" && !hidden[k].includes(name)) hidden[k].push(name);
    });
  });
  const effect = (what, list) => {
    if (!list.length) return "";
    const every = windows.length > 0 && windows.every((w) => list.includes(SYSTEM_WINDOW_LABELS[w.window] || String(w.window)));
    return every ? `${what} hidden for every period.` : `${what} hidden for: ${escapeHtml(list.join(", "))}.`;
  };
  const effects = [effect("Customer counts are", hidden.customers), effect("Revenue is", hidden.revenue_usd)].filter(Boolean);
  const total = fix.deals_with_any_exception;
  const countKnown = Number.isInteger(total) && total >= 0;
  const anyItem = groups.hubspot.length || groups.platform.length || groups.other.length;

  const parts = [];
  if (c.crm_data_to_fix_from) {
    // PR-ADS-169A — today's check is running or was interrupted: this list is
    // the last finished check's, labelled with its date.
    parts.push(`<p class="system-card__note">From the last finished daily check (${escapeHtml(String(c.crm_data_to_fix_from).slice(0, 16).replace("T", " "))} UTC); today's check has not finished.</p>`);
  } else if (c.checked_at) parts.push(`<p class="system-card__note">As of the last daily check, ${systemAgo(c.age_hours)}.</p>`);
  if (blockers.length) {
    parts.push(`<p><strong>A data problem on our side</strong> (nothing to do in HubSpot):</p><ul class="system-fix-list">${blockers.join("")}</ul>`);
  }
  if (!countKnown) {
    parts.push(`<p><strong>Unknown</strong> — the daily check could not count the deals that need fixing.</p>`);
  }
  if (groups.hubspot.length) {
    parts.push(`<p><strong>In HubSpot</strong> (for the sales team):</p><ul class="system-fix-list">${groups.hubspot.join("")}</ul>
      <p><button type="button" class="btn btn--secondary" data-system-goto="health">Open the list of deals to fix</button></p>`);
  }
  if (groups.platform.length) {
    parts.push(`<p><strong>On our side</strong> (data sync — nothing to do in HubSpot):</p><ul class="system-fix-list">${groups.platform.join("")}</ul>`);
  }
  if (groups.other.length) {
    parts.push(`<p><strong>Other blockers:</strong></p><ul class="system-fix-list">${groups.other.join("")}</ul>`);
  }
  if (effects.length) parts.push(`<p><strong>Because of this:</strong> ${effects.join(" ")}</p>`);
  if (!windows.length) {
    parts.push(`<p><strong>Unknown</strong> — the daily check did not record which periods show their figures.</p>`);
  } else if (countKnown && total === 0 && !blockers.length && !anyItem && !effects.length) {
    // Said only on a proven zero: a counted total of 0, no blocker of any
    // kind, and every period recorded as shown.
    parts.push(`<p>Nothing to fix — no won deal is blocking a figure.</p>`);
  }
  return `<div class="panel system-card">${header}<div class="system-card__body">${parts.join("")}</div></div>`;
}

const SYSTEM_ADVANCED_LINKS = [
  ["scheduler", "Data Runs", "See when the scheduled updates ran, and start one by hand."],
  ["revenue-health", "Revenue Health", "Match HubSpot campaign names to Google Ads campaigns, and refill missing spend."],
  ["health", "System Status", "Technical detail on every data pipeline, and the list of deals to fix."],
  ["gclid-attribution", "GCLID Attribution", "Click-level Google Ads tracking detail."],
  ["backfill", "Admin Backfill", "Re-import older history."],
];

function renderSystemOverview(data) {
  const d = data || {};
  const cert = d.certification || {};
  const sources = (Array.isArray(d.sources) ? d.sources : []).map(systemSourceLine).join("");
  const links = SYSTEM_ADVANCED_LINKS.map(([page, name, what]) =>
    `<li><a href="#/${page}" data-system-goto="${page}"><strong>${escapeHtml(name)}</strong></a> — ${escapeHtml(what)}</li>`).join("");
  return `${systemCertCard(cert)}
    <div class="panel system-card"><div class="panel__header">Is the data up to date?</div>
      <div style="overflow-x:auto"><table class="data-table"><tbody>${sources
        || '<tr><td>Unknown — no source could be read.</td></tr>'}</tbody></table></div></div>
    ${systemFixCard(cert)}
    <div class="panel system-card"><div class="panel__header">Advanced tools — rarely needed</div>
      <div class="system-card__body"><ul class="system-fix-list">${links}</ul></div></div>`;
}

let _systemReqSeq = 0;

async function loadSystem() {
  const body = document.getElementById("system-body");
  const token = ++_systemReqSeq;
  if (body) body.innerHTML = '<p class="empty-state" style="padding:var(--space-5)">Loading…</p>';
  try {
    const data = await fetchJSON("/api/system/overview");
    if (token !== _systemReqSeq) return;
    if (body) body.innerHTML = renderSystemOverview(data);
  } catch (err) {
    console.error("[loadSystem]", err);
    if (token !== _systemReqSeq) return;
    if (body) body.innerHTML = '<div class="panel"><p class="empty-state" style="padding:var(--space-5)"><strong>Unavailable</strong> — the system status could not be loaded.</p></div>';
  }
}

// ── Source Attribution Backfill panel (admin-only) ──────────────────────────
// PR-ADS-168B: moved from the deleted Revenue by Source page to Revenue Health
// (loadRevenueHealth renders it into #source-backfill-panel). The job and its
// endpoints are unchanged.

let sourceBackfillJobId = null;
let sourceBackfillPollTimer = null;
let sourceBackfillNotice = "";

function renderSourceBackfillPanelInit() {
  const panel = document.getElementById("source-backfill-panel");
  if (!panel) return;
  if (!_currentUser || _currentUser.role !== "admin") { panel.innerHTML = ""; return; }
  loadSourceBackfillStatus();
}

function renderSourceBackfillPanel(status) {
  const panel = document.getElementById("source-backfill-panel");
  if (!panel) return;
  if (!_currentUser || _currentUser.role !== "admin") { panel.innerHTML = ""; return; }
  const st = status || {};
  const running = st.running === true;
  const s = st.summary || null;
  const progressHtml = running
    ? `<div class="recovery-progress">Running… phase: <strong>${escapeHtml(st.phase || "starting")}</strong>${st.current_chunk ? ` · chunk ${escapeHtml(st.current_chunk)}` : ""}</div>`
    : "";
  const resultHtml = s
    ? `<div class="revenue-summary-strip revenue-summary-strip--four">
         <div><span>Contacts classified</span><strong>${fmtCount(s.contacts_classified)}</strong></div>
         <div><span>Deals attributed</span><strong>${fmtCount(s.deals_attributed)}</strong></div>
         <div><span>Ambiguous</span><strong>${fmtCount(s.ambiguous_deals)}</strong></div>
         <div><span>Unclassified / failed</span><strong>${fmtCount((s.unclassified_deals || 0) + (s.failed || 0))}</strong></div>
       </div>` : "";
  const noticeHtml = sourceBackfillNotice ? `<p class="revenue-footnote">${escapeHtml(sourceBackfillNotice)}</p>` : "";
  panel.innerHTML = `
    <div class="panel recovery-panel">
      <div class="panel__header">Source Attribution Backfill</div>
      <div class="panel__body">
        <p class="revenue-footnote">Classifies all historical contacts by HubSpot Original Source and attributes closed-won deals. Reads HubSpot only and writes only to the local database. Default range: All Time.</p>
        <div class="recovery-actions">
          <button type="button" class="btn btn--secondary" data-source-backfill-action="preview" ${running ? "disabled" : ""}>Preview backfill</button>
          <button type="button" class="btn btn--primary" data-source-backfill-action="run" ${running ? "disabled" : ""}>Run backfill</button>
        </div>
        ${progressHtml}
        ${noticeHtml}
        ${resultHtml}
      </div>
    </div>
  `;
}

function stopSourceBackfillPolling() {
  if (sourceBackfillPollTimer) { clearTimeout(sourceBackfillPollTimer); sourceBackfillPollTimer = null; }
}

async function fetchSourceBackfillStatus() {
  const url = sourceBackfillJobId
    ? `/api/source-attribution-backfill/status?job_id=${encodeURIComponent(sourceBackfillJobId)}`
    : "/api/source-attribution-backfill/status";
  const res = await fetch(url, { credentials: "same-origin" });
  if (!res.ok) return null;
  return res.json();
}

async function pollSourceBackfillOnce() {
  if (_currentPage !== "revenue-health") { stopSourceBackfillPolling(); return; }
  try {
    const status = await fetchSourceBackfillStatus();
    if (status) {
      if (status.job_id) sourceBackfillJobId = status.job_id;
      renderSourceBackfillPanel(status);
      if (status.running) {
        sourceBackfillPollTimer = setTimeout(pollSourceBackfillOnce, 2500);
        return;
      }
      // Job finished: refresh Revenue Health (it re-reads this panel's status).
      loadRevenueHealth();
    }
  } catch (err) {
    console.error("[pollSourceBackfill]", err);
  }
  stopSourceBackfillPolling();
}

async function loadSourceBackfillStatus() {
  stopSourceBackfillPolling();
  sourceBackfillNotice = "";
  try {
    const status = await fetchSourceBackfillStatus();
    if (!status) { renderSourceBackfillPanel(null); return; }
    if (status.job_id) sourceBackfillJobId = status.job_id;
    renderSourceBackfillPanel(status);
    if (status.running) sourceBackfillPollTimer = setTimeout(pollSourceBackfillOnce, 2500);
  } catch (err) {
    console.error("[loadSourceBackfillStatus]", err);
    renderSourceBackfillPanel(null);
  }
}

async function runSourceBackfillJob(dryRun) {
  sourceBackfillNotice = "";
  renderSourceBackfillPanel({ running: true, phase: dryRun ? "preview" : "starting" });
  try {
    const res = await fetch("/api/source-attribution-backfill/run", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: dryRun }),
    });
    if (res.status === 409) { sourceBackfillNotice = "A backfill is already running."; loadSourceBackfillStatus(); return; }
    if (!res.ok) { sourceBackfillNotice = "Source backfill could not be started."; renderSourceBackfillPanel(null); return; }
    const accepted = await res.json();
    sourceBackfillJobId = accepted.job_id || null;
    stopSourceBackfillPolling();
    pollSourceBackfillOnce();
  } catch (err) {
    console.error("[runSourceBackfillJob]", err);
    sourceBackfillNotice = "Source backfill request failed.";
    renderSourceBackfillPanel(null);
  }
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-source-backfill-action]");
  if (!btn) return;
  runSourceBackfillJob(btn.dataset.sourceBackfillAction === "preview");
});

// ── Google Ads Spend Truth panel (PR-ADS-118) ──────────────────────────────
// Forensic audit of canonical API / canonical local / legacy geo spend, with an
// admin spend-backfill action. Read-only to Google Ads.

let spendBackfillJobId = null;
let spendBackfillPollTimer = null;
let spendTruthNotice = "";
let fxBackfillNotice = "";  // PR-ADS-119

const SPEND_STATE_LABEL = {
  VERIFIED: "Verified", PARTIAL: "Partial coverage",
  GEO_MISMATCH: "Geo mismatch", UNAVAILABLE: "Unavailable",
};

function renderSpendTruth(audit, backfill) {
  const panel = document.getElementById("spend-truth-panel");
  if (!panel) return;
  const a = audit || {};
  const win = a.window || {};
  const stateLabel = SPEND_STATE_LABEL[a.state] || (a.state || "—");
  const isAdmin = _currentUser && _currentUser.role === "admin";
  const running = backfill && backfill.running === true;
  const bs = backfill && backfill.summary;

  const adminPanel = isAdmin ? `
    <div class="recovery-actions">
      <button type="button" class="btn btn--secondary" data-spend-backfill-action="preview" ${running ? "disabled" : ""}>Preview Google Ads spend backfill</button>
      <button type="button" class="btn btn--primary" data-spend-backfill-action="run" ${running ? "disabled" : ""}>Run Google Ads spend backfill</button>
      <button type="button" class="btn btn--secondary" data-fx-backfill-action="run">Backfill FX rates (GBP→USD)</button>
    </div>
    ${fxBackfillNotice ? `<p class="revenue-footnote">${escapeHtml(fxBackfillNotice)}</p>` : ""}
    ${running ? `<div class="recovery-progress">Running… ${escapeHtml((backfill && backfill.phase) || "starting")}${backfill && backfill.current_chunk ? ` · ${escapeHtml(backfill.current_chunk)}` : ""}</div>` : ""}
    ${spendTruthNotice ? `<p class="revenue-footnote">${escapeHtml(spendTruthNotice)}</p>` : ""}
    ${bs ? `<div class="revenue-summary-strip revenue-summary-strip--four">
        <div><span>Chunks verified</span><strong>${fmtCount(bs.chunks_verified)}</strong></div>
        <div><span>Chunks failed</span><strong>${fmtCount(bs.chunks_failed)}</strong></div>
        <div><span>Rows written</span><strong>${fmtCount(bs.rows_written)}</strong></div>
        <div><span>API spend total (native)</span><strong>${bs.api_total_spend === null || bs.api_total_spend === undefined ? "—" : fmtCurrency(bs.api_total_spend, a.native_currency || "GBP")}</strong></div>
      </div>` : ""}
  ` : "";

  panel.innerHTML = `
    <div class="panel recovery-panel">
      <div class="panel__header">Google Ads Spend Truth</div>
      <div class="panel__body">
        <div class="recovery-statuses">
          <div>Window: <strong>${escapeHtml(win.label || "")}</strong> · ${escapeHtml(win.start_date || "")} → ${escapeHtml(win.end_date || "")}</div>
          <div>State: <strong class="spend-state spend-state--${escapeHtml((a.state || "").toLowerCase())}">${escapeHtml(stateLabel)}</strong></div>
        </div>
        <div class="recovery-statuses">
          <div>Customer: <strong>${escapeHtml(a.customer_id || "—")}</strong> (${escapeHtml(a.native_currency || a.currency_code || "—")})</div>
          <div>Canonical campaigns: <strong>${fmtCount(a.canonical_campaign_count)}</strong></div>
          <div>Geo reconciliation: <strong>${escapeHtml(a.geo_reconciliation_status || "—")}</strong></div>
        </div>
        <div class="revenue-summary-strip revenue-summary-strip--four">
          <div><span>Google Ads native spend</span><strong>${a.native_total === null || a.native_total === undefined ? "Unavailable" : fmtCurrency(a.native_total, a.native_currency || "GBP")}</strong></div>
          <div><span>USD reporting spend</span><strong>${a.usd_total === null || a.usd_total === undefined ? "FX incomplete" : fmtUSD(a.usd_total)}</strong></div>
          <div><span>FX coverage</span><strong class="spend-state spend-state--${a.fx_coverage_complete ? "verified" : "partial"}">${a.fx_coverage_complete ? "Verified" : (a.fx_coverage_state === "UNAVAILABLE" ? "Unavailable" : "Incomplete")}</strong></div>
          <div><span>Legacy geo total (native)</span><strong>${a.legacy_geo_total === null || a.legacy_geo_total === undefined ? "Unavailable" : fmtCurrency(a.legacy_geo_total, a.native_currency || "GBP")}</strong></div>
        </div>
        <div class="recovery-statuses">
          <div>Account time zone: <strong>${escapeHtml(a.account_time_zone || "—")}</strong></div>
        </div>
        <div class="revenue-summary-strip revenue-summary-strip--four">
          <div><span>Direct account daily total</span><strong>${a.account_daily_total === null || a.account_daily_total === undefined ? "Unavailable" : fmtCurrency(a.account_daily_total, a.native_currency || "GBP")}</strong></div>
          <div><span>Campaign daily total</span><strong>${a.campaign_daily_total === null || a.campaign_daily_total === undefined ? "Unavailable" : fmtCurrency(a.campaign_daily_total, a.native_currency || "GBP")}</strong></div>
          <div><span>Variance</span><strong>${a.account_variance_amount === null || a.account_variance_amount === undefined ? "—" : fmtCurrency(a.account_variance_amount, a.native_currency || "GBP")}${a.account_variance_pct != null ? ` (${(a.account_variance_pct * 100).toFixed(2)}%)` : ""}</strong></div>
          <div><span>Account reconciliation</span><strong class="spend-state spend-state--${a.account_reconciliation_status === "verified" ? "verified" : "partial"}">${escapeHtml((a.account_reconciliation_status || "—").replace(/^\w/, (c) => c.toUpperCase()))}</strong></div>
        </div>
        ${(a.account_reconciliation_status === "mismatch") ? `<p class="revenue-footnote">Campaign-daily sum does not yet reconcile to the direct account-daily total for this window — the delta above is shown instead of claiming success.</p>` : ""}
        ${(a.missing_chunks && a.missing_chunks.length) ? `<p class="revenue-footnote">Missing / unverified chunks: ${a.missing_chunks.map((c) => escapeHtml(`${c.start}→${c.end}`)).join(", ")}. Missing rows are never treated as £0 spend.</p>` : ""}
        ${(a.fx_missing_dates && a.fx_missing_dates.length) ? `<p class="revenue-footnote">FX coverage incomplete — missing rates for ${a.fx_missing_dates.length} date(s). USD spend and Period Revenue / Ad Spend are unavailable until FX coverage is complete.</p>` : ""}
        ${adminPanel}
      </div>
    </div>
  `;
}

async function loadSpendTruth() {
  const panel = document.getElementById("spend-truth-panel");
  if (!panel) return;
  stopSpendBackfillPolling();
  spendTruthNotice = "";
  const window_ = getRoasBusinessWindow();
  let audit = null, backfill = null;
  try {
    const res = await fetch(`/api/google-ads-spend-audit?window=${encodeURIComponent(window_)}`, { credentials: "same-origin" });
    if (res.ok) audit = await res.json();
  } catch (err) { console.error("[loadSpendTruth audit]", err); }
  try {
    const bres = await fetch("/api/google-ads-spend-backfill/status", { credentials: "same-origin" });
    if (bres.ok) backfill = await bres.json();
  } catch (_) { /* non-admin or unavailable */ }
  if (backfill && backfill.job_id) spendBackfillJobId = backfill.job_id;
  renderSpendTruth(audit, backfill);
  if (backfill && backfill.running) spendBackfillPollTimer = setTimeout(pollSpendBackfillOnce, 2500);
}

function stopSpendBackfillPolling() {
  if (spendBackfillPollTimer) { clearTimeout(spendBackfillPollTimer); spendBackfillPollTimer = null; }
}

async function pollSpendBackfillOnce() {
  if (_currentPage !== "revenue-health") { stopSpendBackfillPolling(); return; }
  try {
    const url = spendBackfillJobId
      ? `/api/google-ads-spend-backfill/status?job_id=${encodeURIComponent(spendBackfillJobId)}`
      : "/api/google-ads-spend-backfill/status";
    const res = await fetch(url, { credentials: "same-origin" });
    if (res.ok) {
      const backfill = await res.json();
      if (backfill.job_id) spendBackfillJobId = backfill.job_id;
      if (backfill.running) {
        loadSpendTruthRenderOnly(backfill);
        spendBackfillPollTimer = setTimeout(pollSpendBackfillOnce, 2500);
        return;
      }
      loadSpendTruth();  // finished → refresh audit too
    }
  } catch (err) { console.error("[pollSpendBackfill]", err); }
  stopSpendBackfillPolling();
}

async function loadSpendTruthRenderOnly(backfill) {
  // Re-render only the backfill portion while a job runs (keeps last audit).
  const window_ = getRoasBusinessWindow();
  let audit = null;
  try {
    const res = await fetch(`/api/google-ads-spend-audit?window=${encodeURIComponent(window_)}`, { credentials: "same-origin" });
    if (res.ok) audit = await res.json();
  } catch (_) { /* ignore */ }
  renderSpendTruth(audit, backfill);
}

async function runSpendBackfillJob(dryRun) {
  spendTruthNotice = "";
  try {
    const res = await fetch("/api/google-ads-spend-backfill/run", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: dryRun }),
    });
    if (res.status === 409) { spendTruthNotice = "A spend backfill is already running."; loadSpendTruth(); return; }
    if (!res.ok) { spendTruthNotice = "Spend backfill could not be started."; loadSpendTruth(); return; }
    const accepted = await res.json();
    spendBackfillJobId = accepted.job_id || null;
    stopSpendBackfillPolling();
    pollSpendBackfillOnce();
  } catch (err) {
    console.error("[runSpendBackfillJob]", err);
    spendTruthNotice = "Spend backfill request failed.";
    loadSpendTruth();
  }
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-spend-backfill-action]");
  if (!btn) return;
  runSpendBackfillJob(btn.dataset.spendBackfillAction === "preview");
});

// ── PR-ADS-119: FX rate backfill + campaign identity mapping review ──────────

async function runFxBackfill() {
  fxBackfillNotice = "Starting FX backfill…";
  loadSpendTruthRenderOnly(null);
  try {
    const res = await fetch("/api/fx-backfill/run", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    if (res.status === 409) { fxBackfillNotice = "An FX backfill is already running."; }
    else if (!res.ok) { fxBackfillNotice = "FX backfill could not be started."; }
    else { fxBackfillNotice = "FX backfill started — daily GBP→USD rates are being fetched."; }
  } catch (err) {
    console.error("[runFxBackfill]", err);
    fxBackfillNotice = "FX backfill request failed.";
  }
  loadSpendTruth();
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-fx-backfill-action]");
  if (!btn) return;
  runFxBackfill();
});

let _lastCampaignMappingReview = null;
let campaignMappingNotice = null; // { type: "ok"|"error"|"info", msg }

async function loadCampaignMapping() {
  const panel = document.getElementById("campaign-mapping-panel");
  if (!panel) return;
  const window_ = getRoasBusinessWindow();
  let review = null;
  try {
    const res = await fetch(`/api/campaign-mapping-review?window=${encodeURIComponent(window_)}`, { credentials: "same-origin" });
    if (res.ok) review = await res.json();
  } catch (err) { console.error("[loadCampaignMapping]", err); }
  _lastCampaignMappingReview = review;
  renderCampaignMapping(review);
}

const _MAPPING_STATUS_TEXT = {
  matched: "Auto (exact)", exact_normalized: "Auto (exact)", manual: "Manual mapping",
  not_google_ads: "Not Google Ads", unmatched: "Unmatched",
};

function mappingReasonLabel(reason) {
  switch (reason) {
    case "exact_normalized": return "Exact match";
    case "fuzzy_name":       return "Fuzzy name match";
    case "active_spend":     return "Active spend";
    case "historical_spend": return "Historical spend";
    case "zero_spend_known_identity": return "Known identity";
    case "manual":           return "Manual";
    case "not_google_ads":   return "Excluded";
    default:                 return reason || "—";
  }
}

// Native + USD spend preview line for a selected/candidate Google Ads campaign.
function mappingSpendPreview(native, usd, nativeCur) {
  const nat = (native === null || native === undefined || native === "")
    ? "—" : fmtCurrency(Number(native), nativeCur);
  const u = (usd === null || usd === undefined || usd === "")
    ? "Unavailable" : fmtUSD(Number(usd));
  return `Native spend: <strong>${nat}</strong> · USD spend: <strong>${u}</strong>`;
}

function mappingOptionHtml(c, nativeCur) {
  if (!c || !c.campaign_id) return "";
  const name = c.canonical_campaign_name || "(unnamed campaign)";
  const native = (c.native_spend === null || c.native_spend === undefined) ? "" : c.native_spend;
  const usd = (c.usd_spend === null || c.usd_spend === undefined) ? "" : c.usd_spend;
  return `<option value="${escapeHtml(String(c.campaign_id))}" data-name="${escapeHtml(name)}" data-native="${native}" data-usd="${usd}">${escapeHtml(name)}</option>`;
}

function renderCampaignMappingCard(row, nativeCur, customerId, options) {
  const status = row.status || row.match_status || "unmatched";
  const isExcluded = status === "not_google_ads";
  const isResolved = status === "matched" || status === "manual";
  // Dropdown source: per-row scored candidates (suggestions first) when present,
  // otherwise the full controlled campaign list (e.g. for an excluded row).
  const candidates = (row.candidates && row.candidates.length) ? row.candidates : options;
  const optionHtml = (candidates || []).map((c) => mappingOptionHtml(c, nativeCur)).join("");

  // Suggested candidates: exact + strong fuzzy matches only.
  const suggestions = (row.candidates || []).filter(
    (c) => c.match_reason === "exact_normalized" || c.match_reason === "fuzzy_name"
  ).slice(0, 3);
  const suggestHtml = suggestions.length ? suggestions.map((c) => `
    <button type="button" class="mapping-chip" data-mapping-action="suggest"
            data-campaign-id="${escapeHtml(String(c.campaign_id || ""))}">
      ${escapeHtml(c.canonical_campaign_name || "")}
      <span class="mapping-chip__meta">${mappingReasonLabel(c.match_reason)} · ${Math.round((c.match_score || 0) * 100)}%</span>
    </button>`).join("")
    : `<span class="mapping-chip mapping-chip--none">No strong suggestions — pick from the full list.</span>`;

  // Initial native/USD spend preview: the currently mapped campaign (resolved
  // rows) or the top suggested candidate (unmatched rows). Never a fake $0.
  let previewNative = row.native_spend, previewUsd = row.usd_spend;
  if (!isResolved && row.candidates && row.candidates.length) {
    previewNative = row.candidates[0].native_spend;
    previewUsd = row.candidates[0].usd_spend;
  }

  const leadsTxt = (row.leads === null || row.leads === undefined) ? "—" : fmtCount(row.leads);
  const sqlsTxt = (row.sqls === null || row.sqls === undefined) ? "—" : fmtCount(row.sqls);

  // PR-ADS-177A-F3: these are the LEGACY `leads` table's counts (the mapping
  // workbench reads `fetch_lead_quality`), labelled as such — never "SQLs".
  const metrics = `
    <div class="mapping-card__metrics">
      <span>Revenue <strong>${rowMetricHtml(row, "revenue_usd", fmtUSD, businessBlockerOpts())}</strong></span>
      <span>Leads · legacy leads table <strong>${leadsTxt}</strong></span>
      <span>${LEAD_STATUS_QUALIFIED_LABEL} <strong>${sqlsTxt}</strong></span>
      <span>Won deals <strong>${rowMetricHtml(row, "closed_won_deals", fmtCount, businessBlockerOpts())}</strong></span>
      <span>Customers <strong>${rowMetricHtml(row, "customers", fmtCount, businessBlockerOpts())}</strong></span>
    </div>`;

  const head = `
    <div class="mapping-card__head">
      <div class="mapping-card__label">${escapeHtml(row.external_campaign_label || "")}</div>
      <span class="mapping-status mapping-status--${escapeHtml(status)}">${escapeHtml(_MAPPING_STATUS_TEXT[status] || status)}</span>
    </div>`;

  if (isExcluded) {
    return `
      <div class="mapping-card mapping-card--excluded" data-label="${escapeHtml(row.external_campaign_label || "")}">
        ${head}
        ${metrics}
        <div class="mapping-excluded-note">Not Google Ads — excluded from Google Ads campaign revenue and spend.</div>
        <div class="mapping-card__controls">
          <div class="mapping-picker">
            <input type="text" class="mapping-search" data-mapping-search placeholder="Search campaigns…" aria-label="Search Google Ads campaigns" />
            <select class="mapping-select" data-mapping-select aria-label="Google Ads campaign">
              <option value="">Re-map to a Google Ads campaign…</option>
              ${optionHtml}
            </select>
          </div>
          <button type="button" class="btn btn--secondary" data-mapping-action="approve">Save mapping</button>
        </div>
      </div>`;
  }

  const resolvedNote = isResolved ? `
    <div class="mapping-resolved-note">Mapped to <strong>${escapeHtml(row.canonical_campaign_name || "—")}</strong>${status === "matched" ? " (auto exact match)" : ""}.</div>` : "";

  return `
    <div class="mapping-card mapping-card--${escapeHtml(status)}" data-label="${escapeHtml(row.external_campaign_label || "")}">
      ${head}
      ${metrics}
      ${resolvedNote}
      <div class="mapping-card__suggest">
        <span class="mapping-card__suggest-label">Suggested Google Ads campaigns:</span>
        ${suggestHtml}
      </div>
      <div class="mapping-card__controls">
        <div class="mapping-picker">
          <input type="text" class="mapping-search" data-mapping-search placeholder="Search campaigns…" aria-label="Search Google Ads campaigns" />
          <select class="mapping-select" data-mapping-select aria-label="Google Ads campaign">
            <option value="">${isResolved ? "Change mapping…" : "Select Google Ads campaign…"}</option>
            ${optionHtml}
          </select>
        </div>
        <div class="mapping-card__spend" data-mapping-spend>${mappingSpendPreview(previewNative, previewUsd, nativeCur)}</div>
        <div class="mapping-card__confidence">Confidence: <strong>${escapeHtml(row.confidence || "—")}</strong> · ${escapeHtml(mappingReasonLabel(row.match_reason))}</div>
        <div class="mapping-card__actions">
          <button type="button" class="btn btn--primary" data-mapping-action="approve">Approve mapping</button>
          <button type="button" class="btn btn--secondary" data-mapping-action="exclude">Mark as Not Google Ads</button>
        </div>
      </div>
    </div>`;
}

function renderCampaignMapping(review) {
  const panel = document.getElementById("campaign-mapping-panel");
  if (!panel) return;
  const isAdmin = _currentUser && _currentUser.role === "admin";
  if (!isAdmin) { panel.innerHTML = ""; return; }
  const r = review || {};
  const rows = r.rows || [];
  const nativeCur = r.native_currency || "GBP";
  const customerId = r.customer_id || "";
  const options = r.campaign_options || [];

  const unmatched = rows.filter((row) => (row.status || row.match_status) === "unmatched");
  const resolved = rows.filter((row) => {
    const s = row.status || row.match_status;
    return s === "matched" || s === "manual" || s === "not_google_ads";
  });

  const noticeHtml = campaignMappingNotice ? `
    <div class="mapping-notice mapping-notice--${escapeHtml(campaignMappingNotice.type)}">${escapeHtml(campaignMappingNotice.msg)}</div>` : "";

  const cardsHtml = rows.length
    ? (unmatched.concat(resolved)).map((row) => renderCampaignMappingCard(row, nativeCur, customerId, options)).join("")
    : `<p class="empty-state">No HubSpot campaign labels in this window.</p>`;

  panel.innerHTML = `
    <div class="panel recovery-panel" data-campaign-mapping data-customer-id="${escapeHtml(customerId)}">
      <div class="panel__header">Campaign Identity Mapping Review</div>
      <div class="panel__body">
        <div class="recovery-statuses">
          <div>Customer: <strong>${escapeHtml(customerId || "—")}</strong></div>
          <div>Unmatched labels: <strong>${fmtCount(r.unmatched_count)}</strong></div>
        </div>
        ${noticeHtml}
        <div class="mapping-cards">${cardsHtml}</div>
        <p class="revenue-footnote">Map identity only: HubSpot label → Google Ads campaign ID. Spend is sourced only from canonical Google Ads spend — no manual spend entry. Exact normalized matches auto-link; unmatched labels show "Spend mapping unavailable" (never $0). "Mark as Not Google Ads" excludes a label from Google Ads campaign revenue and spend. Mappings are auditable (approved_by / approved_at) and never overwrite the raw Google Ads campaign identity.</p>
      </div>
    </div>
  `;
}

function setCampaignMappingNotice(type, msg) {
  campaignMappingNotice = { type, msg };
  if (_lastCampaignMappingReview) renderCampaignMapping(_lastCampaignMappingReview);
}

async function saveCampaignMapping(label, campaignId, campaignName, customerId) {
  campaignMappingNotice = { type: "info", msg: `Saving mapping for "${label}"…` };
  if (_lastCampaignMappingReview) renderCampaignMapping(_lastCampaignMappingReview);
  try {
    const res = await fetch("/api/campaign-mapping", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        customer_id: customerId,
        external_campaign_label: label,
        campaign_id: campaignId,
        canonical_campaign_name: campaignName,
        historical_campaign_name: label,
      }),
    });
    if (res.status === 401 || res.status === 403) {
      setCampaignMappingNotice("error", "You must be an admin to save mappings.");
      return;
    }
    if (!res.ok) {
      setCampaignMappingNotice("error", "Mapping could not be saved.");
      return;
    }
    campaignMappingNotice = { type: "ok", msg: `Mapped "${label}" → ${campaignName}. Refresh Campaigns to see it applied.` };
  } catch (err) {
    console.error("[saveCampaignMapping]", err);
    setCampaignMappingNotice("error", "Mapping request failed.");
    return;
  }
  loadCampaignMapping();
}

async function excludeCampaignLabel(label, customerId) {
  campaignMappingNotice = { type: "info", msg: `Marking "${label}" as Not Google Ads…` };
  if (_lastCampaignMappingReview) renderCampaignMapping(_lastCampaignMappingReview);
  try {
    const res = await fetch("/api/campaign-mapping/exclude", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ customer_id: customerId, external_campaign_label: label }),
    });
    if (res.status === 401 || res.status === 403) {
      setCampaignMappingNotice("error", "You must be an admin to change mappings.");
      return;
    }
    if (!res.ok) {
      setCampaignMappingNotice("error", "Label could not be excluded.");
      return;
    }
    campaignMappingNotice = { type: "ok", msg: `"${label}" excluded from Google Ads campaign revenue and spend.` };
  } catch (err) {
    console.error("[excludeCampaignLabel]", err);
    setCampaignMappingNotice("error", "Exclude request failed.");
    return;
  }
  loadCampaignMapping();
}

// Campaign mapping workbench — click (approve / exclude / suggestion chip).
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-mapping-action]");
  if (!btn) return;
  const action = btn.dataset.mappingAction;
  const card = btn.closest(".mapping-card");
  const panel = btn.closest("[data-campaign-mapping]");
  if (!card || !panel) return;
  const label = card.dataset.label;
  const customerId = panel.dataset.customerId || "";
  if (action === "suggest") {
    const sel = card.querySelector("[data-mapping-select]");
    if (sel) {
      sel.value = btn.dataset.campaignId || "";
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    }
    return;
  }
  if (action === "approve") {
    const sel = card.querySelector("[data-mapping-select]");
    const opt = sel && sel.selectedOptions[0];
    if (!sel || !sel.value) {
      setCampaignMappingNotice("error", `Choose a Google Ads campaign for "${label}" first.`);
      return;
    }
    saveCampaignMapping(label, sel.value, opt ? (opt.dataset.name || opt.textContent.trim()) : "", customerId);
    return;
  }
  if (action === "exclude") {
    excludeCampaignLabel(label, customerId);
  }
});

// Searchable dropdown — filter options as the admin types.
document.addEventListener("input", (e) => {
  const inp = e.target.closest("[data-mapping-search]");
  if (!inp) return;
  const card = inp.closest(".mapping-card");
  const sel = card && card.querySelector("[data-mapping-select]");
  if (!sel) return;
  const q = inp.value.trim().toLowerCase();
  Array.from(sel.options).forEach((o) => {
    if (!o.value) return; // keep the placeholder visible
    o.hidden = !!q && !o.textContent.toLowerCase().includes(q);
  });
});

// Update the native/USD spend preview when a different campaign is selected.
document.addEventListener("change", (e) => {
  const sel = e.target.closest("[data-mapping-select]");
  if (!sel) return;
  const card = sel.closest(".mapping-card");
  const preview = card && card.querySelector("[data-mapping-spend]");
  if (!preview) return;
  const opt = sel.selectedOptions[0];
  const nativeCur = (_lastCampaignMappingReview && _lastCampaignMappingReview.native_currency) || "GBP";
  if (!opt || !sel.value) {
    preview.innerHTML = mappingSpendPreview(null, null, nativeCur);
    return;
  }
  preview.innerHTML = mappingSpendPreview(opt.dataset.native, opt.dataset.usd, nativeCur);
});

// ── Country spend reasons (shared by the Countries page and Dashboard) ──────
//
// PR-ADS-176B: the Countries evidence page (#/geo, and its read of the geo
// evidence endpoint) is deleted —
// with its map, its summary strip and its lead-quality columns (the Leads page
// owns lead quality). Its truth rules live on in the one Countries page and in
// Dashboard → Countries: spend is Unavailable WITH its reason, never $0 and
// never "—"; a sub-dollar spend reads "<$1". These reason tables are theirs.

// The words for a country reason code (a row's spend reason, a gate gap, a
// withheld total), or null when the code is not a country one.
const COUNTRY_TOTAL_REASONS = {
  residual_not_a_market: "the residual is not a market: its spend and its deals are two unrelated populations",
  country_geo_not_ready: "country spend does not reconcile with campaign spend for this window",
  country_row_figure_unknown: "a country's figure is unknown",
  // The mart's gate gaps (google_ads_geo_sync_service), as named on the card.
  campaign_coverage_incomplete: "canonical campaign spend does not cover the whole window",
  campaign_spend_unreadable: "canonical campaign spend could not be read",
  fx_coverage_incomplete: "an FX rate is missing for part of the window",
  missing_geo_dates: "some days have campaign spend but no country spend",
  campaign_spend_without_geo: "some campaigns have spend but no country spend",
  comparison_not_like_for_like: "the campaign and country totals span different accounts or currencies",
  totals_differ: "country spend does not reconcile with campaign spend",
};
function countryReasonLabel(reason) {
  if (!reason) return null;
  return GEO_ROW_SPEND_REASONS[reason] || GEO_COVERAGE_REASONS[reason]
    || COUNTRY_TOTAL_REASONS[reason] || null;
}

const GEO_COVERAGE_REASONS = {
  geo_coverage_incomplete: "not every day of the window has been synced",
  // Every day has been fetched at least once, but the newest was last fetched
  // while it was still in progress: what is stored for it is a partial day.
  spend_not_reread_since_day_ended: "yesterday's spend has not been re-read since the day ended",
  // The same, but a sync covering those days has already run and FAILED.
  spend_reread_failed: "the sync that should have re-read yesterday's spend failed",
  window_includes_day_in_progress: "the window includes today, whose spend is still arriving",
  account_time_zone_unknown: "the Google Ads account's time zone is not on record, so it cannot be told when each day ended",
  no_settled_day_in_window: "this window has no completed day yet",
  geo_coverage_has_failed_chunks: "a sync of part of this window failed and has not been repeated",
  geo_coverage_ledger_unreadable: "the record of which days were synced could not be read",
  geo_rows_unreadable: "the synced country spend could not be read",
  google_ads_customer_id_not_configured: "no Google Ads account is configured",
  // PR-ADS-176A truth audit MINOR 3: the zero rule withheld a market's $0.
  spend_location_unresolved: "some spend in this window has no country",
};

// PR-ADS-170A: Google Ads reports a few locations with no country at all. The
// owner labelled them (config/geo_owner_labels.yaml); the server marks every
// row whose country rests on that label. The same words on all three country
// pages — the row never passes an owner's label off as Google's.
const OWNER_LABELLED_COUNTRY_NOTE = "Country set by owner — Google Ads reports this location without a country";

/** The small note beside an owner-labelled country name, or "". `provenance`
 *  is the row's `country_provenance` (Countries) or `spend_country_provenance`
 *  (Country Revenue & Spend, Dashboard Countries). */
function ownerLabelledCountryNote(provenance) {
  if (provenance !== "owner_labelled") return "";
  return ` <span class="owner-label-note" title="${escapeHtml(OWNER_LABELLED_COUNTRY_NOTE)}">set by owner</span>`;
}

/** The same note for a Country Revenue & Spend / Dashboard Countries row,
 *  whose geo spend the server marks with `spend_country_provenance`. */
function spendOwnerNote(row) {
  return ownerLabelledCountryNote((row || {}).spend_country_provenance);
}

/** The owner marker as plain text, for a map hover or a tooltip that is not
 *  HTML. `provenance` as for ownerLabelledCountryNote. */
function ownerLabelledCountryText(provenance) {
  return provenance === "owner_labelled" ? " (country set by owner)" : "";
}

/** PR-ADS-170A-F1: what to say when Google Ads now reports a country for a
 *  location the owner labelled differently. The stored Google code is the one
 *  in use; the owner's label is not applied. One sentence per location, "" when
 *  there is none. `block` is the payload's `owner_labelled_geo`. */
function ownerLabelMismatchText(block) {
  const list = Array.isArray((block || {}).mismatches) ? block.mismatches : [];
  if (!list.length) return "";
  const parts = list.map((m) => {
    const codes = Array.isArray(m.stored_codes) ? m.stored_codes.join(", ") : "";
    const many = Array.isArray(m.stored_codes) && m.stored_codes.length > 1;
    return `location ${m.criterion_id}: the owner set ${m.owner_code}, Google Ads reports ${codes}`
      + (many ? " (two different countries, so its spend is under no country)" : ` — ${codes} is used`);
  });
  return `Owner country label not applied — Google Ads now reports a country for ${list.length === 1 ? "a location" : "locations"} the owner labelled differently (${parts.join("; ")}). Google's code always wins. Review the entry in config/geo_owner_labels.yaml.`;
}

/** The same sentence as a notice block for the revenue country pages. */
function ownerLabelMismatchNotice(block) {
  const text = ownerLabelMismatchText(block);
  return text ? `<p class="mapping-notice mapping-notice--warn owner-label-mismatch" role="status">${escapeHtml(text)}</p>` : "";
}

/** PR-ADS-170A-F1, restored by PR-ADS-176B-F1 (truth audit M2) for the one
 *  Countries page: a published $0 that stands only on the owner's labels says
 *  so — whether the country's own carrier is owner-labelled or labelled spend
 *  in the window would otherwise have no country. The row carries
 *  `zero_rests_on_owner_labels` (the mart, the zero rule's evidence). */
function ownerZeroNote(row) {
  const on = Array.isArray((row || {}).zero_rests_on_owner_labels) ? row.zero_rests_on_owner_labels : [];
  if (!on.length) return "";
  const title = `This $0 rests on the owner's country label for Google Ads location${on.length === 1 ? "" : "s"} ${on.join(", ")}. Google Ads reports ${on.length === 1 ? "that location" : "those locations"} without a country; without the label that spend could be this country's and the figure would read "Unavailable".`;
  return ` <span class="owner-label-note" title="${escapeHtml(title)}">owner label</span>`;
}

/** The residual row's Spend cell on Country Revenue & Spend. The row's spend is
 *  a sum of named parts (server: `spend_components`); the part whose country
 *  is KNOWN but is not a listed market is stated, never left inside a number
 *  labelled "Unknown". */
function residualSpendCell(r) {
  const c = (r || {}).spend_components || {};
  const out = c.outside_listed_markets;
  const names = Array.isArray(r.outside_listed_market_countries) ? r.outside_listed_market_countries : [];
  // PR-ADS-176B-F1 (m5): in cents — whole dollars printed "$0 is spend in
  // Cuba" for 39 cents, and the parts did not add up to the row.
  if (!(out > 0)) return escapeHtml(fmtCountrySpendCents(r.spend));
  const rest = (c.geo_unassigned || 0) + (c.country_unidentified || 0);
  return `${escapeHtml(fmtCountrySpendCents(r.spend))}<br><small class="metric-reason">${escapeHtml(fmtCountrySpendCents(out))} is spend in ${names.length ? escapeHtml(names.join(", ")) : "countries"} — known ${names.length === 1 ? "country" : "countries"}, not among the listed markets${rest > 0 ? `; ${escapeHtml(fmtCountrySpendCents(rest))} has no country` : ""}</small>`;
}

const GEO_ROW_SPEND_REASONS = {
  spend_location_unresolved: "some spend in this window has no country",
  country_not_matched: "country not matched",
  no_country_recorded: "no country recorded",
  fx_rate_missing: "FX rate missing",
};

// ── Search Terms evidence page (PR-ADS-144, consolidated in PR-ADS-153D) ────
// The ONE search-term investigation destination. Data comes from the
// /api/search-term-evidence family: durable source_date-bounded
// selected-window aggregates, tri-state review states, reported-search-term-
// spend semantics, complete-population KPIs and server-side pagination.
//
// PR-ADS-153D folded the retired standalone Flagged Waste Terms page in here as
// the Flagged tab. It is not a copy of the old page: it reads the canonical
// deduplicated `search_terms` facts (the old page summed `waste_terms` run
// snapshots and multiplied spend by the number of runs that saw a term).
//
// The only write anywhere on this page is a LOCAL review decision. Nothing here
// adds negatives or writes to Google Ads / HubSpot.

// PR-ADS-177C: one page, "Keywords & Search Terms" (owner decision 3,
// 2026-11-24) — the Keyword Evidence page is its Keywords tab.
let _stTab = "terms";               // terms | flagged | patterns | keywords
const ST_TABS = ["terms", "flagged", "patterns", "keywords"];
// Tab requested by a redirect before the page module has rendered (e.g. the
// retired #/waste route landing on Flagged).
let _stPendingTab = null;

function stSetPendingTab(tab) {
  if (ST_TABS.includes(tab)) _stPendingTab = tab;
}

let _flagData = null;                // /api/search-term-evidence/flagged payload
let _flagLoadState = "idle";
let _flagFilters = { q: "", campaign: "", reviewState: "", reason: "",
                     minSpend: "", sqlState: "", sort: "priority", page: 1 };
let _flagReqId = 0;
let _flagDebounceTimer = null;
// Term to focus when arriving from a deep link that names one
// (#/search-terms?tab=flagged&term=…).
let _flagFocusTerm = null;
let _stData = null;                  // /api/search-term-evidence payload
let _stLoadState = "idle";           // idle | loading | ok | db_unavailable | error
let _stFilters = { q: "", campaign: "", state: "", junk: "", sqlState: "", minSpend: "", sort: "spend", page: 1 };
let _stReqId = 0;
let _stDebounceTimer = null;

let _patData = null;                 // /api/search-term-evidence/patterns payload
let _patLoadState = "idle";
let _patFilters = { n: "1", q: "", campaign: "", state: "", minSpend: "", minTerms: "", sort: "spend" };
let _patReqId = 0;
let _patDebounceTimer = null;
let _patPendingCampaignName = null;  // campaign-drawer prefill (by display name)

let _stDrawerKeyHandler = null;

const ST_STATE_LABELS = {
  flagged: "Flagged waste",
  clean: "Reviewed clean",
  needs_review: "Needs review",
};
const ST_STATE_TONE = { flagged: "bad", clean: "good", needs_review: "warn" };

const ST_SIGNAL_LABELS = {
  flagged_present: "Flagged present",
  needs_review: "Needs review",
  mixed: "Mixed",
  reviewed_clean_only: "Reviewed clean only",
};
const ST_SIGNAL_TONE = {
  flagged_present: "bad",
  needs_review: "warn",
  mixed: "warn",
  reviewed_clean_only: "good",
};

function stStateBadge(state) {
  const tone = ST_STATE_TONE[state] || "muted";
  const label = ST_STATE_LABELS[state] || fmt(state);
  return `<span class="campaign-status-badge campaign-status-badge--${tone}">${label}</span>`;
}

function stSignalBadge(signal) {
  const tone = ST_SIGNAL_TONE[signal] || "muted";
  const label = ST_SIGNAL_LABELS[signal] || fmt(signal);
  return `<span class="campaign-status-badge campaign-status-badge--${tone}">${label}</span>`;
}

// Exact reported money — never a fabricated $0 for a null.
function stMoney(v) {
  if (v === null || v === undefined) return "—";
  return "$" + Number(v).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
}

function stSpendCell(row) {
  // Currency-aware spend display: prefers verified USD, falls back to native
  // with disclosure. The 78 legacy rows (unverified currency) stay visible with
  // an explicit marker and never a fabricated $0 (PR-ADS-145 §3).
  if (row.spend_usd !== null && row.spend_usd !== undefined) return stMoney(row.spend_usd);
  if (row.legacy_currency_unverified) {
    return `<span class="st-legacy-marker" title="Legacy row — currency lineage unverified; monetary value withheld, never assumed GBP/USD">Unverified</span>`;
  }
  if (row.spend_native !== null && row.spend_native !== undefined) {
    const cur = row.native_currency || "?";
    const val = Number(row.spend_native).toLocaleString(undefined, {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
    return `<span title="USD unavailable — FX incomplete">${cur} ${val}</span>`;
  }
  return "—";
}

function stUnavailable() {
  return `<span class="detail-unavailable">Unavailable</span>`;
}

function stKpiCard(label, valueHtml, sub) {
  return `<div class="dash-kpi-card">
    <div class="dash-kpi-card__label">${label}</div>
    <div class="dash-kpi-card__value">${valueHtml}</div>
    ${sub ? `<div class="dash-kpi-card__sub">${sub}</div>` : ""}
  </div>`;
}

function stWindowLabel() {
  const w = getEvidenceWindow();
  return (typeof CAMPAIGN_WINDOW_LABELS !== "undefined" && CAMPAIGN_WINDOW_LABELS[w]) || w;
}

// ── Page entry point ─────────────────────────────────────────────────────────

function loadSearchTermsEvidence() {
  renderPageDatasetFreshness("search_terms");
  _stFilters.page = 1;   // fresh page entry / window change → restart pagination
  _flagFilters.page = 1;
  // Deep links: #/search-terms?tab=patterns (also produced by the #/ngrams
  // alias) and ?tab=flagged (also produced by the retired #/waste route).
  const rawHash = window.location.hash || "";
  const hashParams = new URLSearchParams(rawHash.split("?")[1] || "");
  const hashTab = hashParams.get("tab");
  if (rawHash.startsWith("#/ngrams")) {
    _stTab = "patterns";
  } else if (ST_TABS.includes(hashTab)) {
    _stTab = hashTab;
  }
  // A redirect (e.g. #/waste → Flagged) sets the tab before this module runs.
  if (_stPendingTab) {
    _stTab = _stPendingTab;
    _stPendingTab = null;
  }
  // A deep link may carry the term it wants investigated, so it lands on the
  // row it is about rather than on a generic list.
  // URLSearchParams.get() already decodes, so this value is used as-is.
  // Decoding again would throw URIError on a legitimate term such as "100%".
  const focusTerm = hashParams.get("term");
  if (focusTerm) {
    _flagFocusTerm = focusTerm;
    _flagFilters.q = focusTerm;
  }
  // Campaign-drawer prefill (navigate("ngrams") with a campaign context).
  if (ngramsPrefill && ngramsPrefill.campaign) {
    _patPendingCampaignName = ngramsPrefill.campaign;
    _stTab = "patterns";
    ngramsPrefill = null;
  }
  renderSearchTermsShell();
  stLoadActiveTab();
}

function stLoadActiveTab() {
  if (_stTab === "patterns") loadPatternsTab();
  else if (_stTab === "flagged") loadFlaggedTab();
  else if (_stTab === "keywords") loadKeywordsTab();
  else loadTermsTab();
}

// Back-compat entry point (older callers/tests referenced loadSearchTerms).
function loadSearchTerms() { loadSearchTermsEvidence(); }

function renderSearchTermsShell() {
  const shell = document.getElementById("search-terms-shell");
  if (!shell) return;
  // PR-ADS-177C: the page header and command bar are in index.html (the
  // Dashboard design, as Campaigns); the shell holds the window chip, the
  // four tabs and the active tab's body.
  shell.innerHTML = `
    <div class="dash-command-bar kst-command-bar">
      <div class="dash-command-bar__truth">
        <span class="dash-chip" title="Selected evidence window">Window: ${escapeHtml(stWindowLabel())}</span>
      </div>
    </div>
    <div class="search-terms-tabs" role="tablist" aria-label="Keywords &amp; Search Terms tabs">
      <button id="tab-btn-terms" class="search-terms-tab${_stTab === "terms" ? " active" : ""}"
        role="tab" aria-selected="${_stTab === "terms"}" data-tab="terms" type="button">Terms</button>
      <button id="tab-btn-flagged" class="search-terms-tab${_stTab === "flagged" ? " active" : ""}"
        role="tab" aria-selected="${_stTab === "flagged"}" data-tab="flagged" type="button"
        title="Search terms flagged for human review by durable waste evidence">Flagged</button>
      <button id="tab-btn-patterns" class="search-terms-tab${_stTab === "patterns" ? " active" : ""}"
        role="tab" aria-selected="${_stTab === "patterns"}" data-tab="patterns" type="button">Patterns</button>
      <button id="tab-btn-keywords" class="search-terms-tab${_stTab === "keywords" ? " active" : ""}"
        role="tab" aria-selected="${_stTab === "keywords"}" data-tab="keywords" type="button"
        title="Google Ads keyword activity, match-type exposure and quality diagnostics">Keywords</button>
    </div>
    <div id="st-tab-body"></div>`;
  shell.querySelectorAll(".search-terms-tab").forEach((tab) => {
    tab.addEventListener("click", () => stActivateTab(tab.dataset.tab));
  });
}

function stActivateTab(tab) {
  if (!ST_TABS.includes(tab)) return;
  _stTab = tab;
  document.querySelectorAll(".search-terms-tab").forEach((t) => {
    const active = t.dataset.tab === tab;
    t.classList.toggle("active", active);
    t.setAttribute("aria-selected", String(active));
  });
  // Switching tabs by hand clears an inbound term focus, so the filter cannot
  // silently persist into a view the user did not ask for it in.
  _flagFocusTerm = null;
  // Keep the deep link honest without triggering the router.
  const newHash = tab === "terms" ? "#/search-terms" : `#/search-terms?tab=${tab}`;
  if (window.location.hash !== newHash) {
    history.replaceState(history.state, "", newHash);
  }
  stLoadActiveTab();
}

// ── Terms tab ────────────────────────────────────────────────────────────────

function stBuildTermsParams() {
  const p = new URLSearchParams();
  p.set("window", getEvidenceWindow());
  p.set("page", String(_stFilters.page));
  p.set("page_size", "50");
  p.set("sort", _stFilters.sort || "spend");
  if (_stFilters.q) p.set("q", _stFilters.q);
  if (_stFilters.campaign) p.set("campaign", _stFilters.campaign);
  if (_stFilters.state) p.set("state", _stFilters.state);
  if (_stFilters.junk) p.set("junk_category", _stFilters.junk);
  if (_stFilters.sqlState) p.set("sql_state", _stFilters.sqlState);
  if (_stFilters.minSpend !== "" && _stFilters.minSpend !== null) {
    p.set("min_spend", String(_stFilters.minSpend));
  }
  return p;
}

async function loadTermsTab() {
  const reqId = ++_stReqId;
  _stLoadState = "loading";
  renderTermsTab();
  try {
    const data = await fetchJSON(`/api/search-term-evidence?${stBuildTermsParams().toString()}`);
    if (reqId !== _stReqId || _stTab !== "terms") return;
    _stData = data;
    _stLoadState = data.db_unavailable ? "db_unavailable" : "ok";
  } catch (err) {
    if (reqId !== _stReqId || _stTab !== "terms") return;
    console.error("search-term-evidence load failed", err);
    _stLoadState = "error";
  }
  renderTermsTab();
}

function stHasActiveFilters() {
  return Boolean(_stFilters.q || _stFilters.campaign || _stFilters.state ||
    _stFilters.junk || _stFilters.minSpend !== "");
}

function renderTermsTab() {
  const body = document.getElementById("st-tab-body");
  if (!body || _stTab !== "terms") return;

  if (_stLoadState === "loading" && !_stData) {
    body.innerHTML = `<div class="evidence-empty-state">Loading search terms…</div>`;
    return;
  }
  if (_stLoadState === "db_unavailable") {
    body.innerHTML = `<div class="evidence-empty-state">
      Data unavailable — the search_terms source cannot be reached right now.
      No metrics are shown because none can be verified.
      <a href="#/health">Check System Status</a>.</div>`;
    return;
  }
  if (_stLoadState === "error") {
    body.innerHTML = `<div class="evidence-empty-state">
      Search terms could not be loaded. Retry, or check System Status.</div>`;
    return;
  }
  if (!_stData) return;

  body.innerHTML = `
    ${renderTermsKPIs(_stData.kpis)}
    ${renderTermsFilters(_stData.facets)}
    ${stSqlCoverageNote()}
    <div class="evidence-table-shell">
      <div class="evidence-table-scroll" id="st-terms-table"></div>
      <div class="st-pagination" id="st-terms-pagination"></div>
    </div>`;
  renderTermsTable();
  wireTermsControls();
}

// Compact Complete / Partial / Unavailable disclosure for the Verified
// Search-Term Spend KPI (PR-ADS-145 §5/§7). No extra banner or pill row —
// just a subline on the card.
// Accurate exclusion phrase for a verified subtotal. A Partial status can come
// from legacy rows with unverified currency AND/OR rows whose FX is incomplete
// for a source_date — never conflate the two (a partial-by-FX window would
// otherwise read "excludes 0 legacy rows"). PR-ADS-145 §7.
function stExclusionPhrase(legacy, fxIncomplete) {
  legacy = legacy || 0;
  fxIncomplete = fxIncomplete || 0;
  const total = legacy + fxIncomplete;
  if (!total) return "excludes unverified rows";
  if (legacy && fxIncomplete) {
    return `excludes ${fmtCount(total)} unverified rows ` +
      `(${fmtCount(legacy)} legacy · ${fmtCount(fxIncomplete)} FX-incomplete)`;
  }
  if (fxIncomplete) {
    return `excludes ${fmtCount(fxIncomplete)} FX-incomplete row${fxIncomplete === 1 ? "" : "s"}`;
  }
  return `excludes ${fmtCount(legacy)} legacy row${legacy === 1 ? "" : "s"}`;
}

function stVerifiedSpendCard(kpis) {
  const mon = kpis.monetary || {};
  const status = kpis.monetary_status || mon.monetary_completeness_status || "unavailable";
  const usd = kpis.verified_spend_usd;
  if (status === "unavailable" || usd === null || usd === undefined) {
    return stKpiCard("Verified Search-Term Spend", stUnavailable(),
      "No verified currency lineage for this window");
  }
  const nativeSub = (mon.verified_native_spend !== null && mon.verified_native_spend !== undefined)
    ? `${escapeHtml(mon.native_currency || "GBP")} ${Number(mon.verified_native_spend).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})} native`
    : "";
  // The Complete/Partial badge lives in the subline (never inline with the big
  // value, which would clip) — PR-ADS-145 §7 compact disclosure.
  const badge = status === "partial"
    ? `<span class="st-mon-badge st-mon-badge--partial" title="Verified subtotal — excludes rows with unverified currency or incomplete FX">Partial</span>`
    : `<span class="st-mon-badge st-mon-badge--complete" title="All rows verified and FX-complete">Complete</span>`;
  let sub;
  if (status === "partial") {
    sub = `${badge} ${stExclusionPhrase(mon.unverified_currency_units, mon.fx_incomplete_units)}` +
      (nativeSub ? ` · ${nativeSub}` : "");
  } else {
    sub = `${badge} ${nativeSub ? `${nativeSub} · FX-converted USD` : "FX-converted USD"}`;
  }
  return stKpiCard("Verified Search-Term Spend", stMoney(usd), sub);
}

function renderTermsKPIs(kpis) {
  if (!kpis) return "";
  const coverage = kpis.coverage || {};
  const val = (v) => (v === null || v === undefined) ? stUnavailable() : fmtCount(v);
  const cards = [
    stKpiCard("Reported Terms", val(kpis.reported_terms), "Term × campaign rows in window"),
    stVerifiedSpendCard(kpis),
    stKpiCard("Clicks", val(kpis.clicks), ""),
    stKpiCard("Flagged Waste", val(kpis.flagged_waste), "Durable flag or confirmed waste evidence"),
    stKpiCard("Needs Review", val(kpis.needs_review), "Not yet classified — absence of evidence is not clean"),
  ];
  // Coverage is a WINDOW-LEVEL source-completeness diagnostic — it does not
  // narrow with the row filters, so showing it next to filtered KPIs would be
  // internally inconsistent. Hide the card whenever filters are active.
  if (coverage.status === "ok" && coverage.coverage_pct !== null &&
      coverage.coverage_pct !== undefined && !stHasActiveFilters()) {
    const verifiedOnly = coverage.scope === "verified_only";
    cards.push(stKpiCard(
      verifiedOnly ? "Verified-row Coverage" : "Reporting Coverage",
      `${Number(coverage.coverage_pct).toFixed(1)}%`,
      (verifiedOnly
        ? `Verified rows only — ${stExclusionPhrase(coverage.excluded_unverified_units, coverage.excluded_fx_incomplete_units)} · of `
        : "Search-term reporting coverage of ") +
      `${stMoney(coverage.canonical_spend_usd)} canonical spend`));
  }
  return `<div class="evidence-kpi-grid st-kpi-grid">${cards.join("")}</div>`;
}

function stCampaignOptions(facets, selected) {
  const opts = [`<option value="">All campaigns</option>`];
  (facets && facets.campaigns ? facets.campaigns : []).forEach((c) => {
    const marker = c.mapping_status === "unmatched" ? " (mapping review)"
      : c.mapping_status === "not_google_ads" ? " (not Google Ads)" : "";
    opts.push(`<option value="${escapeHtml(c.campaign_key)}"${c.campaign_key === selected ? " selected" : ""}>` +
      `${escapeHtml(c.campaign_name || c.campaign_key)}${marker}</option>`);
  });
  return opts.join("");
}

function stStateOptions(selected) {
  return `<option value="">All review states</option>
    <option value="flagged"${selected === "flagged" ? " selected" : ""}>Flagged waste</option>
    <option value="clean"${selected === "clean" ? " selected" : ""}>Reviewed clean</option>
    <option value="needs_review"${selected === "needs_review" ? " selected" : ""}>Needs review</option>`;
}

function renderTermsFilters(facets) {
  const junkOpts = [`<option value="">All junk categories</option>`];
  (facets && facets.junk_categories ? facets.junk_categories : []).forEach((j) => {
    junkOpts.push(`<option value="${escapeHtml(j)}"${j === _stFilters.junk ? " selected" : ""}>${escapeHtml(j)}</option>`);
  });
  return `<div class="campaign-controls st-controls">
    <input type="search" id="st-q" class="waste-search-input" placeholder="Search terms…"
      value="${escapeHtml(_stFilters.q)}" aria-label="Search by term" />
    <select id="st-campaign" class="waste-filter-select" aria-label="Filter by canonical campaign">
      ${stCampaignOptions(facets, _stFilters.campaign)}</select>
    <select id="st-state" class="waste-filter-select" aria-label="Filter by review state">
      ${stStateOptions(_stFilters.state)}</select>
    <select id="st-junk" class="waste-filter-select" aria-label="Filter by junk category">
      ${junkOpts.join("")}</select>
    <select id="st-sqlstate" class="waste-filter-select" aria-label="Filter by SQL attribution">
      <option value=""${!_stFilters.sqlState ? " selected" : ""}>All SQL attribution</option>
      <option value="has_sql"${_stFilters.sqlState === "has_sql" ? " selected" : ""}>Has attributed SQL</option>
      <option value="known_zero"${_stFilters.sqlState === "known_zero" ? " selected" : ""}>Known zero</option>
      <option value="unavailable"${_stFilters.sqlState === "unavailable" ? " selected" : ""}>Unavailable</option>
    </select>
    <input type="number" id="st-min-spend" min="0" step="1" class="waste-search-input st-min-spend"
      placeholder="Min spend $" value="${escapeHtml(String(_stFilters.minSpend))}"
      aria-label="Minimum reported spend in USD" />
    <select id="st-sort" class="waste-filter-select" aria-label="Sort">
      <option value="spend"${_stFilters.sort === "spend" ? " selected" : ""}>Highest spend</option>
      <option value="clicks"${_stFilters.sort === "clicks" ? " selected" : ""}>Most clicks</option>
      <option value="cpc"${_stFilters.sort === "cpc" ? " selected" : ""}>Highest CPC</option>
      <option value="conversions"${_stFilters.sort === "conversions" ? " selected" : ""}>Most platform conversions</option>
      <option value="attributed_sqls"${_stFilters.sort === "attributed_sqls" ? " selected" : ""}>Highest attributed SQLs</option>
      <option value="last_seen"${_stFilters.sort === "last_seen" ? " selected" : ""}>Newest seen</option>
      <option value="term"${_stFilters.sort === "term" ? " selected" : ""}>Search term</option>
    </select>
    <button id="st-reset" type="button" class="revenue-filter-chip campaign-clear-btn">Reset</button>
    <button id="st-export" type="button" class="revenue-filter-chip campaign-clear-btn"
      title="Export the complete server-filtered dataset for this window">Export CSV</button>
  </div>`;
}

// §8 — Attributed-SQL cell. Direct persisted query only; missing evidence is "—",
// never a fabricated zero.
function stSqlCell(r) {
  const st = r.sql_attribution_status;
  const n = r.attributed_sqls;
  if (st === "attributed" && n > 0) {
    return `<span class="kw-sql kw-sql--pos" title="Confirmed attribution: ${escapeHtml(String(n))} HubSpot-confirmed SQL contact(s) with this exact user query">${fmtCount(n)}</span>`;
  }
  if (st === "known_zero") {
    return `<span class="kw-sql kw-sql--zero" title="Proven zero: attribution coverage is complete and no qualified contact carried this exact query in the window">0</span>`;
  }
  if (st === "partial_attribution") {
    return `<span class="kw-sql kw-sql--na" title="Unavailable — exact-query attribution coverage is incomplete this window, so a zero cannot be proven">—</span>`;
  }
  return `<span class="kw-sql kw-sql--na" title="Search-term-level SQL attribution unavailable — the CRM stores a keyword, not the actual user query">—</span>`;
}

// §4/§9 — compact search-term SQL coverage disclosure (window-scoped). Exact-query
// evidence and unique attribution are DISTINCT counts and are shown separately —
// the attributed count is never described as exact-query evidence.
function stSqlCoverageNote() {
  const s = _stData && _stData.sql_attribution;
  if (!s) return "";
  if (!s.sql_attribution_available) {
    return `<div class="kw-sql-note" role="note">Search-term SQL attribution unavailable — HubSpot qualified-contact evidence could not be loaded for this window.</div>`;
  }
  const total = s.sql_total_contacts;
  if (total === null || total === undefined) return "";
  const withQuery = s.sql_contacts_with_exact_search_term || 0;
  const attr = (s.uniquely_attributed_search_term_sql_contacts !== undefined
    && s.uniquely_attributed_search_term_sql_contacts !== null)
    ? s.uniquely_attributed_search_term_sql_contacts
    : (s.sql_attributed_count || 0);
  const extra = s.exact_query_evidence_available
    ? ""
    : " The CRM stores a HubSpot keyword, not the actual user query, so exact search-term attribution is unavailable.";
  return `<div class="kw-sql-note" role="note">`
    + `Exact query evidence: <strong>${fmtCount(withQuery)} of ${fmtCount(total)}</strong> qualified contacts`
    + ` · Uniquely attributed to a search term: <strong>${fmtCount(attr)} of ${fmtCount(total)}</strong>.`
    + `${extra}</div>`;
}

// §8 — HubSpot SQL attribution section for the search-term drawer. Explains
// plainly when term-level attribution is unavailable (CRM stores a keyword only).
function stDrawerSqlSection(s) {
  if (!s) return "";
  const statusLabel = {
    attributed: "Attributed", known_zero: "Known zero",
    mapping_review: "Mapping review", unavailable: "Unavailable",
  }[s.sql_attribution_status] || "Unavailable";
  const count = (s.attributed_sqls === null || s.attributed_sqls === undefined)
    ? "—" : fmtCount(s.attributed_sqls);
  const cov = (s.sql_attribution_coverage_pct === null || s.sql_attribution_coverage_pct === undefined)
    ? "—" : `${s.sql_attribution_coverage_pct}%`;
  const contacts = s.contacts || [];
  const rows = contacts.map((c) => `<tr>
    <td>${escapeHtml(c.company || "—")}</td>
    <td>${escapeHtml(c.contact_id || "—")}</td>
    <td>${escapeHtml(c.sql_date || "—")}</td>
    <td>${escapeHtml(c.campaign_label || "—")}</td>
    <td>${c.has_gclid ? "Yes" : "No"}</td>
  </tr>`).join("");
  return `
    <div class="drawer-section">
      <div class="drawer-section__title">HubSpot SQL Attribution</div>
      <table class="drawer-table st-fact-table"><tbody>
        <tr><th>Attributed SQLs</th><td class="td--num">${count}</td></tr>
        <tr><th>Status</th><td>${escapeHtml(statusLabel)}</td></tr>
        <tr><th>Campaign identity</th><td>${escapeHtml(s.campaign_identity_status || "—")}</td></tr>
        <tr><th>Window coverage</th><td class="td--num">${cov}</td></tr>
      </tbody></table>
      ${s.explanation ? `<p class="drawer-source-note">${escapeHtml(s.explanation)}</p>` : ""}
      ${contacts.length
        ? `<table class="drawer-table"><thead><tr><th>Company</th><th>Contact ID</th><th>SQL date</th><th>Campaign</th><th>GCLID</th></tr></thead><tbody>${rows}</tbody></table>`
        : `<p class="drawer-source-note">No exact-query qualified contacts for this term in the selected window.</p>`}
    </div>`;
}

function stCampaignCell(r) {
  let html = escapeHtml(r.campaign_name || "(no campaign)");
  if (r.mapping_status === "unmatched") {
    html += ` <span class="campaign-map-note">Mapping review</span>`;
  } else if (r.mapping_status === "not_google_ads") {
    html += ` <span class="campaign-map-note">Not Google Ads</span>`;
  } else if (r.aliases && r.aliases.length) {
    html += ` <span class="campaign-alias-hint" title="Also reported as: ${escapeHtml(r.aliases.join(", "))}">alias</span>`;
  }
  return html;
}

function renderTermsTable() {
  const host = document.getElementById("st-terms-table");
  const pager = document.getElementById("st-terms-pagination");
  if (!host) return;
  const rows = (_stData && _stData.rows) || [];
  const pg = (_stData && _stData.pagination) || {};

  if (!rows.length) {
    host.innerHTML = buildEmptyState({
      pageKey: "search-terms",
      canonicalStatus: getPageCanonicalStatus("search-terms"),
      rowsInWindow: 0,
      filtersActive: stHasActiveFilters(),
    }) + `<p class="st-overlap-note" style="padding: 0 var(--space-4) var(--space-3);">A verified empty window is not proof the account has no waste.</p>`;
    if (pager) pager.innerHTML = "";
    return;
  }

  const body = rows.map((r) => `
    <tr class="campaign-decision-row st-row" tabindex="0" role="button"
        data-st-term="${encodeURIComponent(r.search_term)}"
        data-st-key="${encodeURIComponent(r.campaign_key)}"
        aria-label="Open evidence for ${escapeHtml(r.search_term)}">
      <td class="td--name st-td-term" data-label="Search Term">${escapeHtml(r.search_term)}</td>
      <td data-label="State">${stStateBadge(r.state)}</td>
      <td class="st-td-campaign" data-label="Campaign">${stCampaignCell(r)}</td>
      <td class="td--num" data-label="Spend">${stSpendCell(r)}</td>
      <td class="td--num" data-label="Clicks">${fmtCount(r.clicks)}</td>
      <td class="td--num" data-label="CPC">${stMoney(r.cpc_usd)}</td>
      <td class="td--num" data-label="Attributed SQLs">${stSqlCell(r)}</td>
      <td class="td--num" data-label="Last Seen">${r.last_seen ? escapeHtml(r.last_seen) : "—"}</td>
      <td class="td--action" data-label=""><span class="campaign-open-icon" aria-hidden="true" title="Open evidence">›</span></td>
    </tr>`).join("");

  host.innerHTML = `<table class="data-table campaign-decision-table st-table">
    <thead><tr>
      <th class="td--name">Search Term</th><th>State</th><th>Campaign</th>
      <th class="td--num">Spend</th><th class="td--num">Clicks</th>
      <th class="td--num">CPC</th><th class="td--num">Attributed SQLs</th>
      <th class="td--num">Last Seen</th>
      <th class="td--action"><span class="sr-only">Open evidence</span></th>
    </tr></thead><tbody>${body}</tbody></table>`;

  host.querySelectorAll("[data-st-term]").forEach((tr) => {
    const open = () => openStTermDrawer(
      decodeURIComponent(tr.dataset.stTerm), decodeURIComponent(tr.dataset.stKey));
    tr.addEventListener("click", (e) => { if (!e.target.closest("a")) open(); });
    tr.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });
  });

  if (pager) {
    const total = pg.total_count || 0;
    const from = total === 0 ? 0 : ((pg.page - 1) * pg.page_size) + 1;
    const to = total === 0 ? 0 : Math.min(total, from + (pg.returned_count || 0) - 1);
    pager.innerHTML = `
      <span class="st-pagination__info">Showing ${fmtCount(from)}–${fmtCount(to)} of ${fmtCount(total)} reported terms</span>
      <span class="st-pagination__btns">
        <button id="st-prev" type="button" class="revenue-filter-chip" ${pg.page > 1 ? "" : "disabled"}>‹ Prev</button>
        <button id="st-next" type="button" class="revenue-filter-chip" ${pg.has_more ? "" : "disabled"}>Next ›</button>
      </span>`;
    const prev = document.getElementById("st-prev");
    const next = document.getElementById("st-next");
    if (prev) prev.addEventListener("click", () => { _stFilters.page = Math.max(1, _stFilters.page - 1); loadTermsTab(); });
    if (next) next.addEventListener("click", () => { _stFilters.page += 1; loadTermsTab(); });
  }
}

function wireTermsControls() {
  const q = document.getElementById("st-q");
  const minSpend = document.getElementById("st-min-spend");
  const debounced = () => {
    clearTimeout(_stDebounceTimer);
    _stDebounceTimer = setTimeout(() => {
      _stFilters.q = q ? q.value.trim() : "";
      _stFilters.minSpend = minSpend && minSpend.value !== "" ? minSpend.value : "";
      _stFilters.page = 1;
      loadTermsTab();
    }, 350);
  };
  if (q) q.addEventListener("input", debounced);
  if (minSpend) minSpend.addEventListener("input", debounced);
  [["st-campaign", "campaign"], ["st-state", "state"], ["st-junk", "junk"],
   ["st-sqlstate", "sqlState"], ["st-sort", "sort"]]
    .forEach(([id, key]) => {
      const el = document.getElementById(id);
      if (el) el.addEventListener("change", () => {
        _stFilters[key] = el.value;
        _stFilters.page = 1;
        loadTermsTab();
      });
    });
  const reset = document.getElementById("st-reset");
  if (reset) reset.addEventListener("click", () => {
    _stFilters = { q: "", campaign: "", state: "", junk: "", sqlState: "", minSpend: "", sort: "spend", page: 1 };
    loadTermsTab();
  });
  const exportBtn = document.getElementById("st-export");
  if (exportBtn) exportBtn.addEventListener("click", () => {
    // Complete server-filtered export (never a truncated page).
    const p = stBuildTermsParams();
    p.delete("page"); p.delete("page_size");
    const a = document.createElement("a");
    a.href = `/api/search-term-evidence/export?${p.toString()}`;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
  });
}

// ── Flagged tab (PR-ADS-153D) ───────────────────────────────────────────────
// Replaces the retired standalone Flagged Waste Terms page. A term is here
// because DURABLE evidence flagged it — never because "spend > 0 and SQLs = 0",
// which would brand every term with merely unavailable attribution as waste.
//
// All numbers come from /api/search-term-evidence/flagged, which reads the same
// canonical deduplicated `search_terms` facts as the All Terms tab. The old
// page read `waste_terms` run snapshots and multiplied spend by the number of
// runs that had seen a term.

// PR-ADS-165A: how fresh a flag can be. Waste detection ran in the weekly job
// (retired) and in the monthly job; the monthly job is now its only writer.
// The spend, clicks and dates beside a flag still come from the daily
// search-term sync — only the flag itself is monthly.
const FLAG_DETECTION_CADENCE_NOTE =
  "Waste detection runs once a month, on the 1st, over the previous 30 days. " +
  "A flag here can be up to a month old, and a term first seen since the last " +
  "monthly run has not been checked yet — its absence from this tab is not a " +
  "clean result. Spend, clicks and dates are from the daily search-term sync.";

const FLAG_REVIEW_ACTIONS = [
  { state: "keep",              label: "Keep" },
  { state: "monitor",           label: "Monitor" },
  // Deliberately "candidate": Averroes has no write path to Google Ads and
  // cannot know whether a negative keyword was ever applied.
  { state: "exclude_candidate", label: "Exclude candidate" },
  { state: "resolved",          label: "Resolved" },
];

const FLAG_REVIEW_LABELS = {
  unreviewed: "Unreviewed",
  keep: "Keep",
  monitor: "Monitor",
  exclude_candidate: "Exclude candidate",
  resolved: "Resolved",
};

function flagBuildParams() {
  const p = new URLSearchParams();
  p.set("window", getEvidenceWindow());
  p.set("page", String(_flagFilters.page));
  p.set("page_size", "50");
  p.set("sort", _flagFilters.sort || "priority");
  if (_flagFilters.q) p.set("q", _flagFilters.q);
  if (_flagFilters.campaign) p.set("campaign", _flagFilters.campaign);
  if (_flagFilters.reviewState) p.set("review_state", _flagFilters.reviewState);
  if (_flagFilters.reason) p.set("flag_reason", _flagFilters.reason);
  if (_flagFilters.sqlState) p.set("sql_state", _flagFilters.sqlState);
  if (_flagFilters.minSpend !== "" && _flagFilters.minSpend !== null) {
    p.set("min_spend", String(_flagFilters.minSpend));
  }
  return p;
}

async function loadFlaggedTab() {
  const reqId = ++_flagReqId;
  _flagLoadState = "loading";
  renderFlaggedTab();
  try {
    const data = await fetchJSON(`/api/search-term-evidence/flagged?${flagBuildParams().toString()}`);
    if (reqId !== _flagReqId || _stTab !== "flagged") return;
    _flagData = data;
    _flagLoadState = data.db_unavailable ? "db_unavailable" : "ok";
  } catch (err) {
    if (reqId !== _flagReqId || _stTab !== "flagged") return;
    console.error("flagged search terms load failed", err);
    _flagLoadState = "error";
  }
  renderFlaggedTab();
}

function renderFlaggedTab() {
  const body = document.getElementById("st-tab-body");
  if (!body || _stTab !== "flagged") return;

  if (_flagLoadState === "loading" && !_flagData) {
    body.innerHTML = `<div class="evidence-empty-state">Loading flagged terms…</div>`;
    return;
  }
  if (_flagLoadState === "db_unavailable") {
    body.innerHTML = `<div class="evidence-empty-state">
      Data unavailable — the search_terms source cannot be reached right now.
      No metrics are shown because none can be verified.
      <a href="#/health">Check System Status</a>.</div>`;
    return;
  }
  if (_flagLoadState === "error") {
    body.innerHTML = `<div class="evidence-empty-state">
      Flagged terms could not be loaded. Retry, or check System Status.</div>`;
    return;
  }
  if (!_flagData) return;

  // PR-ADS-153D §32 — a quarantined (mismatch) payload must be UNRENDERABLE as
  // normal decision data. We return before the KPI grid, the filters and the
  // table shell are ever written, so there is no element for renderFlaggedTable
  // or wireFlaggedControls to populate — the warning cannot end up sitting on
  // top of numbers it is warning about.
  if (_flagData.actionable === false) {
    body.innerHTML = renderFlaggedQuarantine(_flagData);
    return;
  }

  body.innerHTML = `
    ${flagTruthNotice(_flagData)}
    ${flagReviewOutageNotice(_flagData)}
    ${renderFlaggedKPIs(_flagData.kpis)}
    ${renderFlaggedFilters(_flagData.facets)}
    ${flagFocusNotice()}
    <div class="evidence-table-shell">
      <div class="evidence-table-scroll" id="st-flagged-table"></div>
      <div class="st-pagination" id="st-flagged-pagination"></div>
    </div>
    <p class="grace-note">Flag evidence comes from durable waste classification, never from
      "spend with no SQLs". Review decisions are stored locally only — Averroes never adds
      negative keywords or writes anything to Google Ads.</p>
    <p class="grace-note" id="st-flagged-cadence-note">${escapeHtml(FLAG_DETECTION_CADENCE_NOTE)}</p>`;
  renderFlaggedTable();
  wireFlaggedControls();
}

// Diagnosis only. Deliberately renders no KPI card, no table, no filter and no
// review control — there is nothing here a reader could act on.
function renderFlaggedQuarantine(data) {
  const q = data.quarantine || {};
  const truth = data.truth_state || {};
  const sample = (q.affected_terms_sample || [])
    .map((t) => `<li><code>${escapeHtml(t)}</code></li>`).join("");
  return `<div class="mapping-notice mapping-notice--error">
      <strong>Reconciliation required — flagged metrics withheld.</strong>
      <p>${escapeHtml(q.detail || "An internal invariant failed for this window.")}</p>
      <p>Counts, rows, filters and review actions are unavailable for this window
         because the flagged contract could not be explained. They are withheld
         rather than shown as normal values.</p>
      ${q.affected_term_count ? `<p>${fmtCount(q.affected_term_count)} affected term(s).</p>` : ""}
      ${sample ? `<details><summary>Diagnostic sample</summary><ul>${sample}</ul></details>` : ""}
      ${(truth.reasons || []).length
        ? `<p class="grace-note">Invariant: ${escapeHtml((truth.reasons || []).join(", "))}</p>` : ""}
    </div>`;
}

// Truth state is disclosed, never silently downgraded into zeros.
function flagTruthNotice(data) {
  const truth = data.truth_state || {};
  if (truth.status === "reconciled" || !truth.status) return "";
  const reasons = (truth.reasons || []).join(", ");
  if (truth.status === "mismatch") {
    return `<div class="mapping-notice mapping-notice--error">Reconciliation required —
      a flagged term carries no reason evidence (${escapeHtml(reasons)}). Counts are withheld
      rather than shown as normal values.</div>`;
  }
  const join = data.annotation_join || {};
  const unresolved = join.legacy_unresolved;
  const extra = unresolved
    ? ` ${fmtCount(unresolved)} historical waste annotation${unresolved === 1 ? "" : "s"} could not be
        matched to one canonical campaign and are shown nowhere rather than guessed onto a term.`
    : "";
  return `<div class="mapping-notice mapping-notice--info">Partial evidence
    (${escapeHtml(reasons)}).${extra}</div>`;
}

function flagReviewOutageNotice(data) {
  if (data.review_state_available !== false) return "";
  return `<div class="mapping-notice mapping-notice--info">
    <strong>Review state unavailable.</strong> The local review store could not be read,
    so no review decision can be shown or changed for these terms, and Review Needed is
    withheld. This is not the same as "unreviewed" — no term has been resolved, reopened
    or dismissed. Review actions are disabled until the store is readable again.</div>`;
}

function flagFocusNotice() {
  if (!_flagFocusTerm) return "";
  return `<p class="grace-note">Filtered to <strong>${escapeHtml(_flagFocusTerm)}</strong> by the
    link that opened this view. <button type="button" class="revenue-filter-chip" id="flag-clear-focus">Show all flagged terms</button></p>`;
}

function renderFlaggedKPIs(kpis) {
  if (!kpis) return "";
  const val = (v) => (v === null || v === undefined) ? stUnavailable() : fmtCount(v);
  const spend = (kpis.flagged_spend_usd === null || kpis.flagged_spend_usd === undefined)
    ? stUnavailable() : stMoney(kpis.flagged_spend_usd);
  // SQL evidence is unavailable, NOT zero, when nothing could be attributed.
  const sqlCard = kpis.sql_evidence_available
    ? stKpiCard(escapeHtml(kpis.sql_evidence_label || "Search-term-attributable SQLs"),
        fmtCount(kpis.sql_evidence || 0),
        `${fmtCount(kpis.terms_with_proven_zero_sqls || 0)} term(s) proven zero · ` +
        `${fmtCount(kpis.terms_with_attribution_unavailable || 0)} unavailable`)
    : stKpiCard(escapeHtml(kpis.sql_evidence_label || "Search-term-attributable SQLs"),
        stUnavailable(),
        "Search-term attribution could not be resolved — this is not zero");
  const cards = [
    stKpiCard("Flagged Terms", val(kpis.flagged_terms),
      "Unique durable term identities currently flagged"),
    stKpiCard("Flagged Spend", spend,
      "Canonical Google Ads spend for those terms in this window"),
    sqlCard,
    stKpiCard("Review Needed", val(kpis.review_needed),
      "Flagged terms with no final local review decision"),
  ];
  return `<div class="evidence-kpi-grid st-kpi-grid">${cards.join("")}</div>`;
}

function flagReasonOptions(facets, selected) {
  const opts = [`<option value="">All flag reasons</option>`];
  ((facets && facets.flag_reasons) || []).forEach((r) => {
    opts.push(`<option value="${escapeHtml(r.reason)}"${r.reason === selected ? " selected" : ""}>` +
      `${escapeHtml(r.reason_label || r.reason)}</option>`);
  });
  return opts.join("");
}

function flagReviewStateOptions(selected) {
  const opts = [`<option value="">All review states</option>`];
  Object.keys(FLAG_REVIEW_LABELS).forEach((k) => {
    opts.push(`<option value="${k}"${k === selected ? " selected" : ""}>${escapeHtml(FLAG_REVIEW_LABELS[k])}</option>`);
  });
  return opts.join("");
}

// The minimum useful filter set (§20) — no control is carried over from the old
// page merely because it existed there.
function renderFlaggedFilters(facets) {
  return `<div class="campaign-controls st-controls">
    <input type="search" id="flag-q" class="waste-search-input" placeholder="Search flagged terms…"
      value="${escapeHtml(_flagFilters.q)}" aria-label="Search flagged terms">
    <select id="flag-campaign" class="waste-filter-select" aria-label="Filter by campaign">
      ${stCampaignOptions(facets, _flagFilters.campaign)}
    </select>
    <select id="flag-review-state" class="waste-filter-select" aria-label="Filter by review state"
      ${_flagData && _flagData.review_state_available === false ? "disabled title=\"Review store unavailable — filtering by review state would return a silently wrong subset\"" : ""}>
      ${flagReviewStateOptions(_flagFilters.reviewState)}
    </select>
    <select id="flag-reason" class="waste-filter-select" aria-label="Filter by flag reason">
      ${flagReasonOptions(facets, _flagFilters.reason)}
    </select>
    <select id="flag-sql-state" class="waste-filter-select" aria-label="Filter by SQL evidence">
      <option value="">Any SQL evidence</option>
      <option value="has_sql"${_flagFilters.sqlState === "has_sql" ? " selected" : ""}>Has attributable SQLs</option>
      <option value="known_zero"${_flagFilters.sqlState === "known_zero" ? " selected" : ""}>Proven zero SQLs</option>
      <option value="unavailable"${_flagFilters.sqlState === "unavailable" ? " selected" : ""}>Attribution unavailable</option>
    </select>
    <input type="number" id="flag-min-spend" class="waste-filter-select" min="0" step="1"
      placeholder="Min spend" value="${escapeHtml(String(_flagFilters.minSpend))}"
      aria-label="Minimum spend">
    <select id="flag-sort" class="waste-filter-select" aria-label="Sort flagged terms">
      <option value="priority"${_flagFilters.sort === "priority" ? " selected" : ""}>Priority</option>
      <option value="spend"${_flagFilters.sort === "spend" ? " selected" : ""}>Spend</option>
      <option value="clicks"${_flagFilters.sort === "clicks" ? " selected" : ""}>Clicks</option>
      <option value="last_seen"${_flagFilters.sort === "last_seen" ? " selected" : ""}>Last seen</option>
      <option value="term"${_flagFilters.sort === "term" ? " selected" : ""}>Term</option>
    </select>
  </div>`;
}

// Attribution evidence: an unavailable attribution renders as "—", never 0.
function flagSqlCell(row) {
  const status = row.sql_attribution_status;
  if (status === "attributed") return fmtCount(row.attributed_sqls || 0);
  if (status === "known_zero") {
    return `<span title="Attribution available for this term and found no qualified SQL">0</span>`;
  }
  return `<span class="detail-unavailable" title="Search-term SQL attribution is unavailable for this term — this is not a proven zero">—</span>`;
}

function flagReasonCell(row) {
  const label = escapeHtml(row.flag_reason_label || row.flag_reason || "—");
  if (!row.flag_reason_unmapped) return label;
  // An unrecognised rule value is surfaced, never folded silently into "Other".
  const raw = (row.raw_junk_categories || []).join(", ");
  return `<span class="campaign-status-badge campaign-status-badge--warn"
    title="This flag reason is not in the shared taxonomy — raw value: ${escapeHtml(raw || "none")}">${label}</span>`;
}

// An unreadable review store is NOT "Unreviewed" — saying so would assert that
// no human has decided, on evidence we do not have.
function flagReviewCell(row) {
  if (row.review_state_status === "unavailable" || row.review_state === null) {
    return `<span class="detail-unavailable" title="${escapeHtml(row.review_state_help || "The local review store could not be read.")}">Review state unavailable</span>`;
  }
  return escapeHtml(FLAG_REVIEW_LABELS[row.review_state] || row.review_state || "—");
}

function flagPriorityCell(row) {
  const band = row.priority_band || "low";
  const why = (row.priority_reasons || [])
    .map((r) => `${r.detail} (+${r.points})`).join("\n");
  return `<span class="campaign-status-badge campaign-status-badge--${band === "high" ? "bad" : band === "medium" ? "warn" : "muted"}"
    title="${escapeHtml(why || "No priority signals")}">${escapeHtml(band)} · ${Number(row.priority_score || 0)}</span>`;
}

function renderFlaggedTable() {
  const host = document.getElementById("st-flagged-table");
  if (!host || !_flagData) return;
  const rows = _flagData.rows || [];
  if (!rows.length) {
    host.innerHTML = `<div class="evidence-empty-state">No flagged search terms match these filters
      in this window. That is a real empty result for the selected filters — not a claim that no
      waste exists.</div>`;
    const pager = document.getElementById("st-flagged-pagination");
    if (pager) pager.innerHTML = "";
    return;
  }

  const body = rows.map((r, i) => `<tr class="clickable-row" data-flag-row="${i}" tabindex="0">
      <td><strong>${escapeHtml(r.search_term || "—")}</strong></td>
      <td>${escapeHtml(r.campaign_name || "—")}</td>
      <td class="td--num">${stSpendCell(r)}</td>
      <td class="td--num">${fmtCount(r.clicks || 0)}</td>
      <td>${flagReasonCell(r)}</td>
      <td class="td--num">${flagSqlCell(r)}</td>
      <td>${flagReviewCell(r)}</td>
      <td>${flagPriorityCell(r)}</td>
      <td>${escapeHtml(r.first_flagged_at ? fmtDate(r.first_flagged_at) : "—")}</td>
      <td>${escapeHtml(r.last_seen ? fmtDate(r.last_seen) : "—")}</td>
    </tr>`).join("");

  host.innerHTML = `<table class="data-table campaign-decision-table st-table">
    <thead><tr>
      <th>Search term</th>
      <th>Campaign</th>
      <th class="td--num">Spend</th>
      <th class="td--num">Clicks</th>
      <th title="Why this term is flagged, from the shared waste-reason taxonomy">Reason flagged</th>
      <th class="td--num" title="Search-term-attributable lifecycle SQLs — a strict attribution subset">SQL evidence</th>
      <th title="Local review decision">Review state</th>
      <th title="Deterministic, explainable priority — hover for the exact components">Priority</th>
      <th>First flagged</th>
      <th title="Latest date Google Ads reported this term">Latest seen</th>
    </tr></thead><tbody>${body}</tbody></table>`;

  host.querySelectorAll("[data-flag-row]").forEach((tr) => {
    const open = () => {
      const row = rows[Number(tr.getAttribute("data-flag-row"))];
      openFlaggedDrawer(row);
    };
    tr.addEventListener("click", open);
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
  });

  const pager = document.getElementById("st-flagged-pagination");
  const pg = _flagData.pagination || {};
  if (pager) {
    const from = ((pg.page || 1) - 1) * (pg.page_size || 50) + 1;
    const to = from + rows.length - 1;
    pager.innerHTML = `
      <button type="button" class="btn btn--secondary" id="flag-prev" ${(pg.page || 1) <= 1 ? "disabled" : ""}>Previous</button>
      <span class="roas-window-range">${from}–${to}${pg.total_count === null || pg.total_count === undefined ? "" : ` of ${Number(pg.total_count).toLocaleString()}`}</span>
      <button type="button" class="btn btn--secondary" id="flag-next" ${pg.has_more ? "" : "disabled"}>Next</button>`;
    const prev = document.getElementById("flag-prev");
    const next = document.getElementById("flag-next");
    if (prev) prev.addEventListener("click", () => { _flagFilters.page = Math.max(1, _flagFilters.page - 1); loadFlaggedTab(); });
    if (next) next.addEventListener("click", () => { _flagFilters.page += 1; loadFlaggedTab(); });
  }
}

function wireFlaggedControls() {
  const q = document.getElementById("flag-q");
  if (q) {
    q.addEventListener("input", (e) => {
      clearTimeout(_flagDebounceTimer);
      const value = e.target.value;
      _flagDebounceTimer = setTimeout(() => {
        _flagFilters.q = value; _flagFilters.page = 1; loadFlaggedTab();
      }, 300);
    });
  }
  const bind = (id, key) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("change", (e) => {
      _flagFilters[key] = e.target.value; _flagFilters.page = 1; loadFlaggedTab();
    });
  };
  bind("flag-campaign", "campaign");
  bind("flag-review-state", "reviewState");
  bind("flag-reason", "reason");
  bind("flag-sql-state", "sqlState");
  bind("flag-min-spend", "minSpend");
  bind("flag-sort", "sort");

  const clearFocus = document.getElementById("flag-clear-focus");
  if (clearFocus) clearFocus.addEventListener("click", () => {
    _flagFocusTerm = null; _flagFilters.q = ""; _flagFilters.page = 1; loadFlaggedTab();
  });
}

// ── Flagged term drawer (§26) ───────────────────────────────────────────────
// Google Ads evidence, CRM evidence, flag evidence and the local review
// decision. No external mutation is offered anywhere in it.
function openFlaggedDrawer(row) {
  if (!row) return;
  const body = openStDrawerShell(escapeHtml(row.search_term || "Search term"));
  if (!body) return;

  const sqlBlock = row.sql_attribution_status === "attributed"
    ? `<tr><td>Attributable lifecycle SQLs</td><td>${fmtCount(row.attributed_sqls || 0)}</td></tr>`
    : row.sql_attribution_status === "known_zero"
      ? `<tr><td>Attributable lifecycle SQLs</td><td>0 <span class="muted">(attribution available, none found)</span></td></tr>`
      : `<tr><td>Attributable lifecycle SQLs</td><td><span class="detail-unavailable">Unavailable — not zero</span></td></tr>`;

  const reasons = (row.flag_reasons || []).map((r) =>
    `<li>${escapeHtml(r.reason_label)}${r.raw_reason ? ` <span class="muted">(rule: ${escapeHtml(r.raw_reason)})</span>` : ""}` +
    `${r.unmapped ? ` <span class="campaign-status-badge campaign-status-badge--warn">unmapped</span>` : ""}</li>`).join("");

  const reviewDisabled = (_flagData && _flagData.review_actions_enabled === false)
    || row.review_state_status === "unavailable";
  const actions = reviewDisabled
    ? `<p class="detail-unavailable">Review actions are unavailable — the local review
         store could not be read. No decision can be recorded until it is readable again.</p>`
    : FLAG_REVIEW_ACTIONS.map((a) =>
        `<button type="button" class="btn btn--secondary" data-flag-review="${a.state}"
          ${row.review_state === a.state ? "disabled" : ""}>${escapeHtml(a.label)}</button>`).join(" ");

  body.innerHTML = `
    <h4>Google Ads evidence</h4>
    <table class="data-table"><tbody>
      <tr><td>Spend</td><td>${stSpendCell(row)}</td></tr>
      <tr><td>Clicks</td><td>${fmtCount(row.clicks || 0)}</td></tr>
      <tr><td>Impressions</td><td>${fmtCount(row.impressions || 0)}</td></tr>
      <tr><td>Campaign</td><td>${escapeHtml(row.campaign_name || "—")}</td></tr>
      <tr><td>Source dates</td><td>${escapeHtml(row.first_seen ? fmtDate(row.first_seen) : "—")} → ${escapeHtml(row.last_seen ? fmtDate(row.last_seen) : "—")}</td></tr>
    </tbody></table>

    <h4>CRM evidence</h4>
    <table class="data-table"><tbody>
      ${sqlBlock}
      <tr><td>Attribution scope</td><td>Search-term-attributable (a strict subset of campaign-attributable, Google Ads-source and all-source lifecycle SQLs)</td></tr>
      <tr><td>Attribution status</td><td>${escapeHtml(row.sql_attribution_status || "unavailable")}</td></tr>
    </tbody></table>

    <h4>Flag evidence</h4>
    <table class="data-table"><tbody>
      <tr><td>Reasons</td><td><ul class="drawer-list">${reasons || "<li>—</li>"}</ul></td></tr>
      <tr><td>Source</td><td>${escapeHtml(row.flag_source || "—")} <span class="muted">(${escapeHtml(row.flag_confidence || "—")})</span></td></tr>
      <tr><td>First flagged</td><td>${escapeHtml(row.first_flagged_at ? fmtDate(row.first_flagged_at) : "—")}</td></tr>
      <tr><td>Latest flagged</td><td>${escapeHtml(row.latest_flagged_at ? fmtDate(row.latest_flagged_at) : "—")}</td></tr>
      <tr><td>Review state</td><td>${flagReviewCell(row)}</td></tr>
    </tbody></table>

    <h4>Action</h4>
    <div class="drawer-actions" id="flag-review-actions">${actions}</div>
    <p class="grace-note">${escapeHtml(row.review_state_help || "")}</p>
    <p class="grace-note">Recording a decision writes to Averroes only. No negative keyword is
      created, no campaign, keyword, bid or budget is changed, and nothing is sent to Google Ads.</p>`;

  body.querySelectorAll("[data-flag-review]").forEach((btn) => {
    btn.addEventListener("click", () => flagRecordReview(row, btn.getAttribute("data-flag-review")));
  });
}

async function flagRecordReview(row, state) {
  const host = document.getElementById("flag-review-actions");
  if (host) host.innerHTML = `<span class="muted">Saving review decision…</span>`;
  try {
    await fetchJSON("/api/search-term-evidence/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        campaign_key: row.campaign_key,
        search_term: row.search_term,
        campaign_name: row.campaign_name,
        review_state: state,
      }),
    });
    closeStDrawer();
    loadFlaggedTab();
  } catch (err) {
    console.error("review decision failed", err);
    if (host) {
      host.innerHTML = `<span class="detail-unavailable">Could not save the review decision. Nothing was changed.</span>`;
    }
  }
}

// ── Patterns tab ─────────────────────────────────────────────────────────────

function stBuildPatternParams() {
  const p = new URLSearchParams();
  p.set("window", getEvidenceWindow());
  p.set("n", String(_patFilters.n || "1"));
  p.set("sort", _patFilters.sort || "spend");
  p.set("limit", "100");
  if (_patFilters.q) p.set("q", _patFilters.q);
  if (_patFilters.campaign) p.set("campaign", _patFilters.campaign);
  if (_patFilters.state) p.set("state", _patFilters.state);
  if (_patFilters.minSpend !== "" && _patFilters.minSpend !== null) p.set("min_spend", String(_patFilters.minSpend));
  if (_patFilters.minTerms !== "" && _patFilters.minTerms !== null) p.set("min_terms", String(_patFilters.minTerms));
  return p;
}

async function loadPatternsTab() {
  const reqId = ++_patReqId;
  _patLoadState = "loading";
  renderPatternsTab();
  try {
    const data = await fetchJSON(`/api/search-term-evidence/patterns?${stBuildPatternParams().toString()}`);
    if (reqId !== _patReqId || _stTab !== "patterns") return;
    _patData = data;
    _patLoadState = data.db_unavailable ? "db_unavailable" : "ok";
    // Campaign-drawer prefill by display name → resolve to a facet key once.
    if (_patPendingCampaignName && _patLoadState === "ok") {
      const want = _patPendingCampaignName.trim().toLowerCase();
      _patPendingCampaignName = null;
      const match = ((data.facets || {}).campaigns || []).find(
        (c) => (c.campaign_name || "").trim().toLowerCase() === want);
      if (match && _patFilters.campaign !== match.campaign_key) {
        _patFilters.campaign = match.campaign_key;
        loadPatternsTab();
        return;
      }
    }
  } catch (err) {
    if (reqId !== _patReqId || _stTab !== "patterns") return;
    console.error("search-term patterns load failed", err);
    _patLoadState = "error";
  }
  renderPatternsTab();
}

function renderPatternsTab() {
  const body = document.getElementById("st-tab-body");
  if (!body || _stTab !== "patterns") return;

  if (_patLoadState === "loading" && !_patData) {
    body.innerHTML = `<div class="evidence-empty-state">Analysing patterns…</div>`;
    return;
  }
  if (_patLoadState === "db_unavailable") {
    body.innerHTML = `<div class="evidence-empty-state">
      Patterns are derived from the Search Terms source, which is unavailable right now —
      no pattern evidence can be produced. <a href="#/health">Check System Status</a>.</div>`;
    return;
  }
  if (_patLoadState === "error") {
    body.innerHTML = `<div class="evidence-empty-state">Patterns could not be loaded. Retry, or check System Status.</div>`;
    return;
  }
  if (!_patData) return;

  body.innerHTML = `
    ${renderPatternKPIs(_patData.kpis)}
    ${renderPatternFilters(_patData.facets)}
    <p class="st-overlap-note">${escapeHtml((_patData.overlap || {}).disclosure ||
      "Pattern rows overlap — the same term can appear in several patterns, so pattern spends are not additive.")}</p>
    <div class="evidence-table-shell">
      <div class="evidence-table-scroll" id="st-patterns-table"></div>
      <div class="st-pagination" id="st-patterns-pagination"></div>
    </div>`;
  renderPatternsTable();
  wirePatternControls();
}

function renderPatternKPIs(kpis) {
  if (!kpis) return "";
  const val = (v) => (v === null || v === undefined) ? stUnavailable() : fmtCount(v);
  const cards = [
    stKpiCard("Patterns Found", val(kpis.patterns_found), ""),
    stKpiCard("Terms Analysed", val(kpis.terms_analysed), "Unique terms in selected window"),
    stKpiCard("Patterns with Flagged Terms", val(kpis.patterns_with_flagged), ""),
    stKpiCard("Patterns Needing Review", val(kpis.patterns_needing_review), ""),
    stKpiCard("Reported Spend Represented",
      (kpis.reported_spend_represented_usd === null || kpis.reported_spend_represented_usd === undefined
        ? stUnavailable() : stMoney(kpis.reported_spend_represented_usd)) +
        (kpis.spend_status === "partial"
          ? ` <span class="st-mon-badge st-mon-badge--partial" title="Verified subtotal — some underlying terms have unverified currency">Partial</span>`
          : ""),
      kpis.spend_status === "partial"
        ? "Verified unique underlying terms only — excludes unverified-currency terms"
        : "Unique underlying terms — not additive across pattern rows"),
  ];
  return `<div class="evidence-kpi-grid st-kpi-grid">${cards.join("")}</div>`;
}

function renderPatternFilters(facets) {
  return `<div class="campaign-controls st-controls">
    <select id="pat-n" class="waste-filter-select" aria-label="Pattern word length">
      <option value="1"${_patFilters.n === "1" ? " selected" : ""}>1 word</option>
      <option value="2"${_patFilters.n === "2" ? " selected" : ""}>2 words</option>
      <option value="3"${_patFilters.n === "3" ? " selected" : ""}>3 words</option>
    </select>
    <input type="search" id="pat-q" class="waste-search-input" placeholder="Filter underlying terms…"
      value="${escapeHtml(_patFilters.q)}" aria-label="Filter underlying search terms" />
    <select id="pat-campaign" class="waste-filter-select" aria-label="Filter by canonical campaign">
      ${stCampaignOptions(facets, _patFilters.campaign)}</select>
    <select id="pat-state" class="waste-filter-select" aria-label="Filter by review state">
      ${stStateOptions(_patFilters.state)}</select>
    <input type="number" id="pat-min-spend" min="0" step="1" class="waste-search-input st-min-spend"
      placeholder="Min spend $" value="${escapeHtml(String(_patFilters.minSpend))}"
      aria-label="Minimum reported term spend in USD" />
    <input type="number" id="pat-min-terms" min="1" step="1" class="waste-search-input st-min-terms"
      placeholder="Min terms" value="${escapeHtml(String(_patFilters.minTerms))}"
      aria-label="Minimum unique terms per pattern" />
    <select id="pat-sort" class="waste-filter-select" aria-label="Sort patterns">
      <option value="spend"${_patFilters.sort === "spend" ? " selected" : ""}>Highest spend</option>
      <option value="terms"${_patFilters.sort === "terms" ? " selected" : ""}>Most terms</option>
      <option value="flagged"${_patFilters.sort === "flagged" ? " selected" : ""}>Most flagged</option>
      <option value="pattern"${_patFilters.sort === "pattern" ? " selected" : ""}>Pattern</option>
    </select>
    <button id="pat-reset" type="button" class="revenue-filter-chip campaign-clear-btn">Reset</button>
  </div>`;
}

function renderPatternsTable() {
  const host = document.getElementById("st-patterns-table");
  const pager = document.getElementById("st-patterns-pagination");
  if (!host) return;
  const rows = (_patData && _patData.rows) || [];
  const pg = (_patData && _patData.pagination) || {};

  if (!rows.length) {
    const filtersActive = Boolean(_patFilters.q || _patFilters.campaign ||
      _patFilters.state || _patFilters.minSpend !== "" || _patFilters.minTerms !== "");
    host.innerHTML = buildEmptyState({
      pageKey: "ngrams",
      canonicalStatus: getPageCanonicalStatus("ngrams"),
      rowsInWindow: 0,
      filtersActive,
    }) + `<p class="st-overlap-note" style="padding: 0 var(--space-4) var(--space-3);">Patterns depend on the Search Terms source — an empty result is not proof the account has no waste.</p>`;
    if (pager) pager.innerHTML = "";
    return;
  }

  const body = rows.map((r) => `
    <tr class="campaign-decision-row st-row" tabindex="0" role="button"
        data-pat="${encodeURIComponent(r.pattern)}" data-pat-n="${r.n}"
        aria-label="Open evidence for pattern ${escapeHtml(r.pattern)}">
      <td class="td--name st-td-term" data-label="Pattern">${escapeHtml(r.pattern)}</td>
      <td data-label="Signal">${stSignalBadge(r.signal)}</td>
      <td class="td--num" data-label="Terms">${fmtCount(r.terms)}</td>
      <td class="td--num" data-label="Flagged">${fmtCount(r.flagged_terms)}</td>
      <td class="td--num" data-label="Clean">${fmtCount(r.clean_terms)}</td>
      <td class="td--num" data-label="Needs Review">${fmtCount(r.needs_review_terms)}</td>
      <td class="td--num" data-label="Reported Spend">${stMoney(r.reported_spend_usd)}</td>
      <td class="td--action" data-label=""><span class="campaign-open-icon" aria-hidden="true" title="Open evidence">›</span></td>
    </tr>`).join("");

  host.innerHTML = `<table class="data-table campaign-decision-table st-table st-patterns-table">
    <thead><tr>
      <th class="td--name">Pattern</th><th>Signal</th>
      <th class="td--num">Terms</th><th class="td--num">Flagged</th>
      <th class="td--num">Clean</th><th class="td--num">Needs Review</th>
      <th class="td--num">Reported Spend</th>
      <th class="td--action"><span class="sr-only">Open evidence</span></th>
    </tr></thead><tbody>${body}</tbody></table>`;

  host.querySelectorAll("[data-pat]").forEach((tr) => {
    const open = () => openStPatternDrawer(decodeURIComponent(tr.dataset.pat), tr.dataset.patN);
    tr.addEventListener("click", (e) => { if (!e.target.closest("a")) open(); });
    tr.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });
  });

  if (pager) {
    pager.innerHTML = `<span class="st-pagination__info">Showing ${fmtCount(pg.returned_count || 0)} of ${fmtCount(pg.total_count || 0)} patterns${pg.has_more ? " — refine filters or raise Min terms to narrow" : ""}</span>`;
  }
}

function wirePatternControls() {
  const q = document.getElementById("pat-q");
  const minSpend = document.getElementById("pat-min-spend");
  const minTerms = document.getElementById("pat-min-terms");
  const debounced = () => {
    clearTimeout(_patDebounceTimer);
    _patDebounceTimer = setTimeout(() => {
      _patFilters.q = q ? q.value.trim() : "";
      _patFilters.minSpend = minSpend && minSpend.value !== "" ? minSpend.value : "";
      _patFilters.minTerms = minTerms && minTerms.value !== "" ? minTerms.value : "";
      loadPatternsTab();
    }, 350);
  };
  [q, minSpend, minTerms].forEach((el) => { if (el) el.addEventListener("input", debounced); });
  [["pat-n", "n"], ["pat-campaign", "campaign"], ["pat-state", "state"], ["pat-sort", "sort"]]
    .forEach(([id, key]) => {
      const el = document.getElementById(id);
      if (el) el.addEventListener("change", () => {
        _patFilters[key] = el.value;
        loadPatternsTab();
      });
    });
  const reset = document.getElementById("pat-reset");
  if (reset) reset.addEventListener("click", () => {
    _patFilters = { n: "1", q: "", campaign: "", state: "", minSpend: "", minTerms: "", sort: "spend" };
    loadPatternsTab();
  });
}

// ── Evidence drawer (terms + patterns share the aside) ───────────────────────

function openStDrawerShell(titleHtml) {
  const overlay = document.getElementById("st-drawer-overlay");
  const drawer = document.getElementById("st-drawer");
  const title = document.getElementById("st-drawer-title");
  const body = document.getElementById("st-drawer-body");
  if (!overlay || !drawer || !title || !body) return null;
  title.innerHTML = titleHtml;
  body.innerHTML = `<p class="empty-state">Loading evidence…</p>`;
  overlay.hidden = false;
  overlay.setAttribute("aria-hidden", "false");
  drawer.hidden = false;
  const closeBtn = document.getElementById("st-drawer-close-btn");
  if (closeBtn) { closeBtn.onclick = closeStDrawer; closeBtn.focus(); }
  overlay.onclick = closeStDrawer;
  _stDrawerKeyHandler = (e) => { if (e.key === "Escape") closeStDrawer(); };
  document.addEventListener("keydown", _stDrawerKeyHandler);
  return body;
}

function closeStDrawer() {
  const overlay = document.getElementById("st-drawer-overlay");
  const drawer = document.getElementById("st-drawer");
  if (overlay) { overlay.hidden = true; overlay.setAttribute("aria-hidden", "true"); }
  if (drawer) drawer.hidden = true;
  if (_stDrawerKeyHandler) {
    document.removeEventListener("keydown", _stDrawerKeyHandler);
    _stDrawerKeyHandler = null;
  }
}

function stDrawerKpi(label, valueHtml) {
  return `<div class="drawer-kpi"><div class="drawer-kpi__label">${label}</div>
    <div class="drawer-kpi__value">${valueHtml}</div></div>`;
}

async function openStTermDrawer(term, campaignKey) {
  const body = openStDrawerShell(escapeHtml(term));
  if (!body) return;
  try {
    const p = new URLSearchParams();
    p.set("term", term);
    p.set("window", getEvidenceWindow());
    if (campaignKey) p.set("campaign_key", campaignKey);
    const data = await fetchJSON(`/api/search-term-evidence/term?${p.toString()}`);
    renderStTermDrawer(body, data);
  } catch (err) {
    console.error("term drawer load failed", err);
    body.innerHTML = `<p class="drawer-empty">Evidence could not be loaded for this term.</p>`;
  }
}

function renderStTermDrawer(body, data) {
  if (data.db_unavailable) {
    body.innerHTML = `<p class="drawer-empty">Data unavailable — the search_terms source cannot be reached.</p>`;
    return;
  }
  if (data._not_found || !data.term) {
    body.innerHTML = `<p class="drawer-empty">This term has no reported rows in the selected window.</p>`;
    return;
  }
  const t = data.term;
  const cls = data.classification || {};
  const plat = data.platform_activity || {};
  const match = data.matching_context || {};
  const daily = data.daily || {};
  const campaigns = data.campaigns || [];

  const windowLabel = escapeHtml(stWindowLabel());
  const factRow = (label, valueHtml) =>
    `<tr><td>${label}</td><td>${valueHtml}</td></tr>`;

  const campaignRows = campaigns.map((c) => `
    <tr>
      <td>${escapeHtml(c.campaign_name || "(no campaign)")}${
        c.mapping_status === "unmatched" ? ` <span class="campaign-map-note">Mapping review</span>`
        : c.mapping_status === "not_google_ads" ? ` <span class="campaign-map-note">Not Google Ads</span>` : ""}</td>
      <td class="td--num">${stMoney(c.spend_usd)}</td>
      <td class="td--num">${fmtCount(c.clicks)}</td>
    </tr>`).join("");

  // Per-date currency truth (PR-ADS-144 §4): each day shows its native cost
  // and its own-date FX-converted USD. A day without an FX rate shows native
  // only (USD "—") — the legacy raw column is never rendered with "$".
  const dailyRows = (daily.rows || []).map((d) => `
    <tr><td>${escapeHtml(d.source_date || "")}</td>
      <td class="td--num">${stSpendCell(d)}</td>
      <td class="td--num">${d.spend_usd !== null && d.spend_usd !== undefined ? stMoney(d.spend_usd) : stUnavailable()}</td>
      <td class="td--num">${fmtCount(d.clicks)}</td>
      <td class="td--num">${fmtCount(d.impressions)}</td></tr>`).join("");

  const mapped = campaigns.length === 1 && campaigns[0].mapping_status === "mapped";

  body.innerHTML = `
    <div class="drawer-section drawer-section--header">
      <div class="drawer-header-meta">${stStateBadge(t.state)}
        <span class="drawer-verdict-reason">Evidence window: ${windowLabel}</span></div>
      <p class="drawer-readonly-note">Read-only evidence — no negative keyword is applied and nothing is written to Google Ads.</p>
    </div>
    <div class="drawer-section">
      <div class="drawer-kpi-grid">
        ${stDrawerKpi("Verified USD", t.spend_usd !== null && t.spend_usd !== undefined ? stMoney(t.spend_usd) : stUnavailable())}
        ${stDrawerKpi("Native", t.spend_native !== null && t.spend_native !== undefined ? `${escapeHtml(t.native_currency || "?")} ${Number(t.spend_native).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}` : (t.legacy_currency_unverified ? `<span class="st-legacy-marker">Unverified</span>` : "—"))}
        ${stDrawerKpi("Clicks", fmtCount(t.clicks))}
        ${stDrawerKpi("CPC (USD)", t.cpc_usd !== null && t.cpc_usd !== undefined ? stMoney(t.cpc_usd) : stUnavailable())}
        ${stDrawerKpi("First Seen", t.first_seen ? escapeHtml(t.first_seen) : "—")}
        ${stDrawerKpi("Last Seen", t.last_seen ? escapeHtml(t.last_seen) : "—")}
      </div>
      <p class="drawer-source-note">Currency status: ${escapeHtml(({verified: "Verified — FX-complete USD", verified_same_currency: "Verified (USD native)", fx_incomplete: "FX incomplete — USD withheld", legacy_currency_unverified: "Legacy — currency lineage unverified", mixed_currency: "Mixed currency — USD withheld", unproven_source: "Unproven source — USD withheld"})[t.currency_status] || t.currency_status || "unknown")}. Native spend is never relabelled as USD.</p>
    </div>
    <div class="drawer-section">
      <div class="drawer-section__title">Campaign & Matching Context</div>
      <table class="drawer-table"><thead><tr><th>Campaign</th><th class="td--num">Spend</th><th class="td--num">Clicks</th></tr></thead>
      <tbody>${campaignRows || `<tr><td colspan="3">—</td></tr>`}</tbody></table>
      <table class="drawer-table st-fact-table"><tbody>
        ${factRow("Ad groups", match.ad_groups && match.ad_groups.length ? escapeHtml(match.ad_groups.join(", ")) : "—")}
        ${factRow("Matched keywords", match.keywords && match.keywords.length ? escapeHtml(match.keywords.join(", ")) : "—")}
        ${factRow("Match types", match.match_types && match.match_types.length ? escapeHtml(match.match_types.join(", ")) : "—")}
        ${factRow("Approved aliases", t.aliases && t.aliases.length ? escapeHtml(t.aliases.join(", ")) : "—")}
      </tbody></table>
    </div>
    <div class="drawer-section">
      <div class="drawer-section__title">Classification Proof</div>
      <table class="drawer-table st-fact-table"><tbody>
        ${factRow("Review state", stStateBadge(cls.state))}
        ${factRow("State source", cls.state_source ? escapeHtml(({durable_flag: "Durable flag (search_terms)", waste_terms: "Confirmed waste evidence (scheduled waste detection)", durable_reviewed_clean: "Durable reviewed clean", unclassified: "Unclassified — needs review"})[cls.state_source] || cls.state_source) : "—")}
        ${factRow("Junk category", cls.junk_categories && cls.junk_categories.length ? escapeHtml(cls.junk_categories.join(", ")) : "—")}
        ${factRow("Matched rule / pattern", cls.matched_patterns && cls.matched_patterns.length ? escapeHtml(cls.matched_patterns.join(", ")) : "—")}
        ${factRow("CRM-junk confirmations", cls.crm_junk_confirmed !== null && cls.crm_junk_confirmed !== undefined ? fmtCount(cls.crm_junk_confirmed) : stUnavailable())}
        ${factRow("Classified on", cls.classification_date ? escapeHtml(cls.classification_date) : stUnavailable())}
        ${factRow("Classification source", cls.classification_source ? escapeHtml(cls.classification_source) : stUnavailable())}
      </tbody></table>
      ${cls.proof_status === "unavailable"
        ? `<p class="drawer-source-note">CRM-junk confirmations, classification date and source are withheld here: waste_terms records only the campaign name, which does not uniquely identify this campaign for this term, so borrowing another campaign's proof is not safe. The review state above still comes from this campaign's own stored evidence.</p>`
        : ""}
      <p class="drawer-source-note">Flagged waste = stored is_flagged_waste true (a human-review candidate, not an approved negative). Reviewed clean = stored false. Needs review = not yet classified.</p>
    </div>
    ${stDrawerSqlSection(data.sql_attribution)}
    <div class="drawer-section">
      <div class="drawer-section__title">Platform Conversions</div>
      <table class="drawer-table st-fact-table"><tbody>
        ${factRow("Platform conversions", plat.conversions !== null && plat.conversions !== undefined ? fmtCount(plat.conversions) : "—")}
      </tbody></table>
      <p class="drawer-source-note">${escapeHtml(plat.disclosure || "Platform event — not a confirmed SQL, customer or closed-won outcome.")}</p>
    </div>
    <div class="drawer-section">
      <div class="drawer-section__title">Daily Evidence (source dates)</div>
      ${dailyRows
        ? `<table class="drawer-table"><thead><tr><th>Date</th><th class="td--num">Native</th><th class="td--num">USD</th><th class="td--num">Clicks</th><th class="td--num">Impr.</th></tr></thead><tbody>${dailyRows}</tbody></table>`
        : `<p class="drawer-empty">No per-day rows reported in this window.</p>`}
      <p class="drawer-source-note">Only dates the source actually reported are shown — missing dates are never fabricated as zero. Each day's USD is converted at that day's own FX rate; a day without a rate shows native only.</p>
    </div>
    <div class="drawer-section">
      <div class="drawer-section__title">Links</div>
      <div class="st-drawer-links">
        ${mapped ? `<button type="button" class="revenue-filter-chip" id="st-link-campaign">Open Campaigns</button>` : ""}
        <button type="button" class="revenue-filter-chip" id="st-link-waste">Open Flagged Waste Terms</button>
        <button type="button" class="revenue-filter-chip" id="st-link-patterns">Patterns with these words</button>
        <button type="button" class="revenue-filter-chip" id="st-link-copy">Copy term</button>
      </div>
    </div>`;

  const linkCampaign = document.getElementById("st-link-campaign");
  if (linkCampaign && mapped) {
    linkCampaign.addEventListener("click", () => {
      const c = campaigns[0];
      closeStDrawer();
      // PR-ADS-177C: Campaign Evidence and its drawer are retired; the one
      // Campaigns page opens, with this campaign's row drawer when the row is
      // on it (matched by name — the page's rows are keyed by campaign name).
      campaignsPageOpenName = c.campaign_name || null;
      navigate("campaigns", { updateHash: true });
    });
  }
  const linkWaste = document.getElementById("st-link-waste");
  if (linkWaste) linkWaste.addEventListener("click", () => {
    closeStDrawer();
    _flagFilters.q = t.search_term;
    _flagFilters.page = 1;
    stActivateTab("flagged");
  });
  const linkPatterns = document.getElementById("st-link-patterns");
  if (linkPatterns) linkPatterns.addEventListener("click", () => {
    closeStDrawer();
    _patFilters.q = t.search_term;
    stActivateTab("patterns");
  });
  const linkCopy = document.getElementById("st-link-copy");
  if (linkCopy) linkCopy.addEventListener("click", () => {
    if (navigator.clipboard) navigator.clipboard.writeText(t.search_term);
    linkCopy.textContent = "Copied";
    setTimeout(() => { linkCopy.textContent = "Copy term"; }, 1200);
  });
}

async function openStPatternDrawer(pattern, n) {
  const body = openStDrawerShell(`${escapeHtml(pattern)} <span class="drawer-verdict-reason">${escapeHtml(String(n))}-word pattern</span>`);
  if (!body) return;
  try {
    const p = stBuildPatternParams();
    p.delete("sort"); p.delete("limit"); p.delete("min_terms");
    p.set("pattern", pattern);
    p.set("n", String(n));
    const data = await fetchJSON(`/api/search-term-evidence/patterns/detail?${p.toString()}`);
    renderStPatternDrawer(body, data);
  } catch (err) {
    console.error("pattern drawer load failed", err);
    body.innerHTML = `<p class="drawer-empty">Evidence could not be loaded for this pattern.</p>`;
  }
}

function renderStPatternDrawer(body, data) {
  if (data.db_unavailable) {
    body.innerHTML = `<p class="drawer-empty">Data unavailable — the search_terms source cannot be reached.</p>`;
    return;
  }
  if (data._not_found || !data.pattern) {
    body.innerHTML = `<p class="drawer-empty">This pattern has no underlying terms in the selected window.</p>`;
    return;
  }
  const pat = data.pattern;
  const plat = data.platform_activity || {};
  const termRows = (data.terms || []).map((t) => `
    <tr>
      <td>${escapeHtml(t.search_term)}</td>
      <td>${stStateBadge(t.state)}</td>
      <td>${escapeHtml((t.campaigns || []).join(", ") || "—")}</td>
      <td class="td--num">${stMoney(t.spend_usd)}</td>
      <td class="td--num">${fmtCount(t.clicks)}</td>
      <td class="td--num">${t.last_seen ? escapeHtml(t.last_seen) : "—"}</td>
    </tr>`).join("");

  body.innerHTML = `
    <div class="drawer-section drawer-section--header">
      <div class="drawer-header-meta">${stSignalBadge(pat.signal)}
        <span class="drawer-verdict-reason">Evidence window: ${escapeHtml(stWindowLabel())}</span></div>
      <p class="drawer-readonly-note">Read-only evidence — patterns are review candidates, never negative-keyword recommendations.</p>
    </div>
    <div class="drawer-section">
      <div class="drawer-kpi-grid">
        ${stDrawerKpi("Unique Terms", fmtCount(pat.terms))}
        ${stDrawerKpi("Flagged", fmtCount(pat.flagged_terms))}
        ${stDrawerKpi("Clean", fmtCount(pat.clean_terms))}
        ${stDrawerKpi("Needs Review", fmtCount(pat.needs_review_terms))}
        ${stDrawerKpi("Reported Spend", stMoney(pat.reported_spend_usd))}
        ${stDrawerKpi("Clicks", fmtCount(pat.clicks))}
      </div>
      <p class="drawer-source-note">${escapeHtml(data.overlap_note || "Totals use unique underlying terms; pattern rows overlap and are not additive.")}</p>
    </div>
    <div class="drawer-section">
      <div class="drawer-section__title">Platform Conversions</div>
      <table class="drawer-table st-fact-table"><tbody>
        <tr><td>Platform conversions</td><td>${plat.conversions !== null && plat.conversions !== undefined ? fmtCount(plat.conversions) : "—"}</td></tr>
      </tbody></table>
      <p class="drawer-source-note">${escapeHtml(plat.disclosure || "Platform event — not a confirmed SQL, customer or closed-won outcome.")}</p>
    </div>
    <div class="drawer-section">
      <div class="drawer-section__title">Campaigns Represented</div>
      <p>${(pat.campaigns || []).length ? escapeHtml(pat.campaigns.join(", ")) : "—"}</p>
    </div>
    <div class="drawer-section">
      <div class="drawer-section__title">Top Underlying Search Terms</div>
      <table class="drawer-table"><thead><tr>
        <th>Term</th><th>State</th><th>Campaign</th>
        <th class="td--num">Spend</th><th class="td--num">Clicks</th><th class="td--num">Last Seen</th>
      </tr></thead><tbody>${termRows || `<tr><td colspan="6">—</td></tr>`}</tbody></table>
      ${data.terms_truncated ? `<p class="drawer-source-note">Showing the top underlying terms by spend — the pattern has more terms than are listed here.</p>` : ""}
    </div>`;
}

// ── GCLID Attribution page ───────────────────────────────────────────────────

function buildGclidAttributionParams({ cursor = null } = {}) {
  const params = new URLSearchParams();
  params.set("days", String(getSelectedDays()));
  params.set("limit", "100");

  const gclid = document.getElementById("gclid-filter-gclid")?.value.trim();
  const campaign = document.getElementById("gclid-filter-campaign")?.value.trim();
  const contactId = document.getElementById("gclid-filter-contact")?.value.trim();
  const dealId = document.getElementById("gclid-filter-deal")?.value.trim();
  const matchStatus = document.getElementById("gclid-filter-status")?.value;

  if (gclid) params.set("gclid", gclid);
  if (campaign) params.set("campaign", campaign);
  if (contactId) params.set("contact_id", contactId);
  if (dealId) params.set("deal_id", dealId);
  if (matchStatus) params.set("match_status", matchStatus);
  if (cursor) params.set("cursor", cursor);

  return params;
}

function _updateGclidAttributionPaginationVisibility() {
  const container = document.querySelector("#page-gclid-attribution .pagination-actions");
  const btn = document.getElementById("gclid-load-more-btn");
  if (!container || !btn) return;
  container.hidden = btn.hidden;
}

function gclidStatusBadge(matchStatus) {
  const normalized = (matchStatus || "unknown").toLowerCase();
  const map = {
    matched: { label: "Matched", cls: "gclid-status-matched" },
    url_fallback: { label: "URL Fallback", cls: "gclid-status-url-fallback" },
    unmatched: { label: "Unmatched", cls: "gclid-status-unmatched" },
    unknown: { label: "Unknown", cls: "gclid-status-unknown" },
  };
  const value = map[normalized] || map.unknown;
  return `<span class="gclid-status-badge ${value.cls}">${value.label}</span>`;
}

function renderGclidAttributionKPIs(rows) {
  const kpiGrid = document.getElementById("gclid-attribution-kpis");
  if (!kpiGrid) return;

  const loadedRows = rows.length;
  const matchedRows = rows.filter((r) => (r.match_status || "").toLowerCase() === "matched").length;
  const fallbackRows = rows.filter((r) => (r.match_status || "").toLowerCase() === "url_fallback").length;
  const unmatchedRows = rows.filter((r) => (r.match_status || "").toLowerCase() === "unmatched").length;
  const loadedDealAmount = rows.reduce((sum, r) => sum + (Number(r.deal_amount_usd) || 0), 0);

  kpiGrid.innerHTML = `
    <div class="kpi-card">
      <div class="kpi-card__label">Loaded Rows</div>
      <div class="kpi-card__value">${loadedRows}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">Matched Rows</div>
      <div class="kpi-card__value">${matchedRows}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">URL Fallback Rows</div>
      <div class="kpi-card__value">${fallbackRows}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">Unmatched Rows</div>
      <div class="kpi-card__value">${unmatchedRows}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">Loaded Deal Amount</div>
      <div class="kpi-card__value">${fmtDollar(loadedDealAmount)}</div>
    </div>`;
}

function renderGclidCoverageCards() {
  const kpiGrid = document.getElementById("gclid-coverage-kpis");
  if (!kpiGrid) return;

  if (gclidCoverageStatus === "unavailable") {
    kpiGrid.innerHTML = `
      <div class="kpi-card">
        <div class="kpi-card__label">Coverage Snapshot</div>
        <div class="kpi-card__value">—</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-card__label">Status</div>
        <div class="kpi-card__value">Unavailable</div>
      </div>`;
    return;
  }

  if (gclidCoverageStatus === "empty") {
    kpiGrid.innerHTML = `
      <div class="kpi-card">
        <div class="kpi-card__label">Coverage Snapshot</div>
        <div class="kpi-card__value">—</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-card__label">Status</div>
        <div class="kpi-card__value">No snapshot</div>
      </div>`;
    return;
  }

  const latest = gclidCoverageRows[0] || {};
  const coveragePct = Number(latest.coverage_pct);

  kpiGrid.innerHTML = `
    <div class="kpi-card">
      <div class="kpi-card__label">Contacts With GCLID</div>
      <div class="kpi-card__value">${fmt(latest.contacts_with_gclid)}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">Contacts Without GCLID</div>
      <div class="kpi-card__value">${fmt(latest.contacts_without_gclid)}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">Coverage %</div>
      <div class="kpi-card__value">${Number.isFinite(coveragePct) ? `${coveragePct.toFixed(1)}%` : "—"}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">Matched Deals</div>
      <div class="kpi-card__value">${fmt(latest.matched_deals)}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">Snapshot Date</div>
      <div class="kpi-card__value">${fmt(latest.snapshot_date)}</div>
    </div>`;
}

function renderGclidAttributionTable() {
  const tableEl = document.getElementById("gclid-attribution-table");
  if (!tableEl) return;

  if (gclidStatus === "db_unavailable") {
    tableEl.innerHTML = `
      <div class="waste-empty-state">
        <p class="empty-state">GCLID attribution temporarily unavailable — database offline.</p>
      </div>`;
    return;
  }

  if (gclidStatus === "cursor_error") {
    tableEl.innerHTML = `
      <div class="waste-empty-state">
        <p class="empty-state">Could not load the next page. Refresh and try again.</p>
      </div>`;
    return;
  }

  if (gclidStatus === "error") {
    tableEl.innerHTML = `
      <div class="waste-empty-state">
        <p class="empty-state">Could not load GCLID attribution. Check API health or run status.</p>
      </div>`;
    return;
  }

  if (!gclidRows.length) {
    tableEl.innerHTML = `
      <div class="waste-empty-state">
        <p class="empty-state">No GCLID attribution rows found for the selected filters.</p>
        <p class="waste-empty-subtext">Rows appear after the GCLID matching pipeline persists attribution evidence locally.</p>
      </div>`;
    return;
  }

  const thead = `
    <thead>
      <tr>
        <th>GCLID</th>
        <th>Match Status</th>
        <th>Company</th>
        <th>Country</th>
        <th>Contact ID</th>
        <th>Deal ID</th>
        <th>Deal Stage</th>
        <th class="td--num">Deal Amount</th>
        <th>Campaign</th>
        <th>Keyword</th>
        <th>Match Type</th>
        <th>Search Term</th>
        <th>First URL</th>
        <th>Contact Created</th>
        <th>Deal Created</th>
      </tr>
    </thead>`;

  const tbody = gclidRows.map((r) => `
    <tr>
      <td class="td--name">${escapeHtml(r.gclid || "—")}</td>
      <td>${gclidStatusBadge(r.match_status)}</td>
      <td>${escapeHtml(r.company || "—")}</td>
      <td>${escapeHtml(r.country || "—")}</td>
      <td>${escapeHtml(r.contact_id || "—")}</td>
      <td>${escapeHtml(r.deal_id || "—")}</td>
      <td>${escapeHtml(r.deal_stage_label || r.deal_stage || "—")}</td>
      <td class="td--num">${r.deal_amount_usd != null ? fmtDollar(r.deal_amount_usd) : "—"}</td>
      <td>${escapeHtml(r.campaign_name || "—")}</td>
      <td>${escapeHtml(r.keyword || "—")}</td>
      <td>${escapeHtml(r.match_type || "—")}</td>
      <td>${escapeHtml(r.search_term || "—")}</td>
      <td>${escapeHtml(r.first_url || "—")}</td>
      <td>${fmtDate(r.contact_created_at)}</td>
      <td>${fmtDate(r.deal_created_at)}</td>
    </tr>`).join("");

  tableEl.innerHTML = `<div class="gclid-table-scroll"><table class="data-table">${thead}<tbody>${tbody}</tbody></table></div>`;
}

async function loadGclidAttribution({ reset = false, cursor = null } = {}) {
  if (gclidStatus === "loading") return;

  if (reset) renderPageDatasetFreshness("gclid_attribution");

  gclidStatus = "loading";

  const tableEl = document.getElementById("gclid-attribution-table");
  const loadMoreBtn = document.getElementById("gclid-load-more-btn");

  if (reset) {
    gclidRows = [];
    gclidNextCursor = null;
    gclidHasMore = false;
    renderGclidAttributionKPIs([]);
    if (tableEl) {
      tableEl.innerHTML = `<p class="empty-state" style="padding:var(--space-5)">Loading GCLID attribution…</p>`;
    }
    if (loadMoreBtn) loadMoreBtn.hidden = true;
    _updateGclidAttributionPaginationVisibility();
  } else if (loadMoreBtn) {
    loadMoreBtn.disabled = true;
    loadMoreBtn.textContent = "Loading…";
  }

  try {
    const params = buildGclidAttributionParams({ cursor });
    const data = await fetchJSON(`/api/gclid-attribution?${params.toString()}`);

    if (data.db_unavailable) {
      gclidStatus = "db_unavailable";
      gclidRows = [];
      gclidNextCursor = null;
      gclidHasMore = false;
      renderGclidAttributionKPIs([]);
      renderGclidAttributionTable();
      if (loadMoreBtn) loadMoreBtn.hidden = true;
      _updateGclidAttributionPaginationVisibility();
      return;
    }

    const newRows = data.rows || [];
    const pagination = data.pagination || {};

    gclidRows = reset ? newRows : gclidRows.concat(newRows);
    gclidNextCursor = pagination.next_cursor || null;
    gclidHasMore = !!pagination.has_more;
    gclidStatus = gclidRows.length ? "ok" : "empty";

    renderGclidAttributionKPIs(gclidRows);
    renderGclidAttributionTable();

    if (loadMoreBtn) {
      if (gclidHasMore) {
        loadMoreBtn.hidden = false;
        loadMoreBtn.disabled = false;
        loadMoreBtn.textContent = "Load more";
      } else {
        loadMoreBtn.hidden = true;
      }
    }
    _updateGclidAttributionPaginationVisibility();
  } catch (err) {
    const isCursorError = err && err.message && err.message.includes("Invalid cursor");
    gclidStatus = isCursorError ? "cursor_error" : "error";
    renderGclidAttributionTable();
    if (loadMoreBtn) {
      const canLoadMore = gclidStatus !== "cursor_error" && !!gclidNextCursor;
      loadMoreBtn.hidden = !canLoadMore;
      loadMoreBtn.disabled = false;
      loadMoreBtn.textContent = "Load more";
    }
    _updateGclidAttributionPaginationVisibility();
  }
}

// ── GCLID Attribution Quality (PR-ADS-048) ──────────────────────────────────

async function loadGclidQuality({ campaign = null } = {}) {
  gclidQualityStatus = "loading";
  renderGclidQuality();

  const params = new URLSearchParams();
  params.set("days", String(getSelectedDays()));

  const campaignFilter =
    campaign || document.getElementById("gclid-filter-campaign")?.value.trim();
  if (campaignFilter) params.set("campaign", campaignFilter);

  try {
    const data = await fetchJSON(`/api/attribution/quality?${params.toString()}`);
    gclidQualityData = data;
    gclidQualityStatus = data.db_unavailable ? "db_unavailable" : "ok";
    renderGclidQuality();
  } catch (_) {
    gclidQualityStatus = "error";
    gclidQualityData = null;
    renderGclidQuality();
  }
}

function _gclidQualityStatusClass(status) {
  const map = {
    good:    "gclid-quality-status-good",
    watch:   "gclid-quality-status-watch",
    weak:    "gclid-quality-status-weak",
    risk:    "gclid-quality-status-risk",
    unknown: "gclid-quality-status-unknown",
  };
  return map[(status || "unknown").toLowerCase()] || map.unknown;
}

function _gclidQualityStatusLabel(status) {
  const map = {
    good:    "Good",
    watch:   "Watch",
    weak:    "Weak",
    risk:    "Risk",
    unknown: "Unknown",
  };
  return map[(status || "unknown").toLowerCase()] || "Unknown";
}

function renderGclidQuality() {
  const container = document.getElementById("gclid-quality-summary");
  if (!container) return;

  if (gclidQualityStatus === "loading" || gclidQualityStatus === "idle") {
    container.innerHTML = `<p class="empty-state">Loading attribution quality…</p>`;
    return;
  }

  if (gclidQualityStatus === "db_unavailable") {
    container.innerHTML = `<p class="empty-state">Attribution quality temporarily unavailable — database offline.</p>`;
    return;
  }

  if (gclidQualityStatus === "error" || !gclidQualityData) {
    container.innerHTML = `<p class="empty-state">Could not load attribution quality signals.</p>`;
    return;
  }

  const data    = gclidQualityData;
  const summary = data.summary || {};
  const rates   = data.rates   || {};
  const signals = data.signals || [];
  const fresh   = data.freshness || null;

  // ── Signal cards ──────────────────────────────────────────────────────────
  const signalCardsHtml = signals.map((sig) => {
    const cls   = _gclidQualityStatusClass(sig.status);
    const label = _gclidQualityStatusLabel(sig.status);
    return `
      <div class="gclid-quality-card ${cls}">
        <div class="gclid-quality-card__status">${escapeHtml(label)}</div>
        <div class="gclid-quality-card__label">${escapeHtml(sig.label || "")}</div>
        <div class="gclid-quality-card__detail">${escapeHtml(sig.detail || "")}</div>
      </div>`;
  }).join("");

  // ── Compact metrics row ───────────────────────────────────────────────────
  const totalRows  = summary.loaded_scope_rows != null ? summary.loaded_scope_rows : "—";
  const matchedPct = rates.matched_rate_pct   != null ? `${Number(rates.matched_rate_pct).toFixed(1)}%`   : "—";
  const fallbackPct = rates.url_fallback_rate_pct != null ? `${Number(rates.url_fallback_rate_pct).toFixed(1)}%` : "—";
  const unmatchedPct = rates.unmatched_rate_pct != null ? `${Number(rates.unmatched_rate_pct).toFixed(1)}%` : "—";
  const dealLinkPct  = rates.deal_link_rate_pct != null ? `${Number(rates.deal_link_rate_pct).toFixed(1)}%`  : "—";
  const amountCovPct = rates.deal_amount_coverage_pct != null ? `${Number(rates.deal_amount_coverage_pct).toFixed(1)}%` : "—";

  const freshnessNote = fresh
    ? `<span class="gclid-quality-freshness-note">Local warehouse freshness: ${escapeHtml(fresh.status || "unknown")}${fresh.last_successful_sync_at ? ` · last sync ${escapeHtml(fmtDate(fresh.last_successful_sync_at))}` : ""}</span>`
    : "";

  container.innerHTML = `
    <div class="gclid-quality-cards">${signalCardsHtml}</div>
    <div class="gclid-quality-metrics">
      <span><strong>${totalRows}</strong> scope rows</span>
      <span><strong>${matchedPct}</strong> matched</span>
      <span><strong>${unmatchedPct}</strong> unmatched</span>
      <span><strong>${fallbackPct}</strong> URL fallback</span>
      <span><strong>${dealLinkPct}</strong> deal-linked</span>
      <span><strong>${amountCovPct}</strong> amount coverage</span>
      ${freshnessNote}
    </div>`;
}

async function loadGclidCoverageForAttribution() {
  gclidCoverageStatus = "loading";
  gclidCoverageRows = [];
  renderGclidCoverageCards();

  try {
    const data = await fetchJSON(`/api/gclid-coverage?days=${getSelectedDays()}`);
    if (data.db_unavailable) {
      gclidCoverageStatus = "unavailable";
      gclidCoverageRows = [];
      renderGclidCoverageCards();
      return;
    }

    const rows = data.rows || [];
    if (!rows.length) {
      gclidCoverageStatus = "empty";
      gclidCoverageRows = [];
      renderGclidCoverageCards();
      return;
    }

    const snapshotTs = (row) => {
      const value = row?.created_at || row?.snapshot_date;
      const ts = value ? new Date(value).getTime() : NaN;
      return Number.isFinite(ts) ? ts : -Infinity;
    };

    const latest = rows.reduce((currentLatest, row) => {
      if (!currentLatest) return row;
      const currentTs = snapshotTs(currentLatest);
      const rowTs = snapshotTs(row);
      return rowTs > currentTs ? row : currentLatest;
    }, null);

    gclidCoverageStatus = "ok";
    gclidCoverageRows = latest ? [latest] : [];
    renderGclidCoverageCards();
  } catch (_) {
    gclidCoverageStatus = "unavailable";
    gclidCoverageRows = [];
    renderGclidCoverageCards();
  }
}

// ── Keyword Evidence page (PR-ADS-146) — since PR-ADS-177C, the Keywords tab
//    of the one Keywords & Search Terms page ──────────────────────────────────
// Read-only. Durable keyword_daily_facts via /api/keyword-evidence:
// source_date selected-window totals (never SUM'd over overlapping snapshots),
// per-source-date FX verified spend, latest-observed quality, factual review
// signals.
let _kwData = null;
let _kwLoadState = "idle";
let _kwFilters = { q: "", campaign: "", matchType: "", status: "", quality: "",
                   signal: "", sqlState: "", minSpend: "", sort: "spend", page: 1 };
let _kwReqId = 0;
let _kwDebounce = null;
let _kwDrawerKeyHandler = null;
// All-time completeness metadata from /api/keyword-evidence/history
// (PR-ADS-146A §6). Populated on load; drives the "Partial historical
// coverage" note vs. a complete all-time state.
let _kwHistory = null;

const KW_SIGNAL_TONE = {
  "Broad-match exposure": "warn",
  "Low quality evidence": "warn",
  "Spend without platform conversion": "warn",
  "No recent activity": "neutral",
  "Limited data": "neutral",
  "No platform concern detected": "ok",
  "Quality unavailable": "neutral",
  "Currency unavailable": "neutral",
};

function kwSignalBadge(signal) {
  if (!signal) return "—";
  const tone = KW_SIGNAL_TONE[signal] || "neutral";
  return `<span class="kw-signal kw-signal--${tone}">${escapeHtml(signal)}</span>`;
}

function kwMatchBadge(mt) {
  const label = mt || "UNKNOWN";
  const cls = { BROAD: "broad", PHRASE: "phrase", EXACT: "exact" }[label] || "unknown";
  return `<span class="kw-match kw-match--${cls}">${escapeHtml(label[0] + label.slice(1).toLowerCase())}</span>`;
}

function kwQualityCell(row) {
  // Latest-observed quality. NULL (unavailable) is distinct from a genuine 0.
  if (row.quality_score === null || row.quality_score === undefined) {
    return `<span class="kw-quality kw-quality--na" title="Latest quality score unavailable — not a poor score">Unavailable</span>`;
  }
  const band = row.quality_band || "medium";
  return `<span class="kw-quality kw-quality--${band}" title="Latest observed Quality Score">${row.quality_score}</span>`;
}

function kwSpendCell(row) {
  // Verified USD, else native disclosure, else an Unverified marker — never $0.
  if (row.spend_usd !== null && row.spend_usd !== undefined) return stMoney(row.spend_usd);
  if (row.spend_native !== null && row.spend_native !== undefined) {
    const cur = row.native_currency || "?";
    const val = Number(row.spend_native).toLocaleString(undefined,
      { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return `<span title="USD unavailable — FX incomplete">${escapeHtml(cur)} ${val}</span>`;
  }
  return `<span class="st-legacy-marker" title="Currency lineage unverified — monetary value withheld, never assumed GBP/USD">Unverified</span>`;
}

function kwPct(v) { return (v === null || v === undefined) ? "—" : `${Number(v).toFixed(1)}%`; }
function kwNum(v) { return (v === null || v === undefined) ? "—" : fmtCount(v); }

function kwWindowLabel() {
  const w = getEvidenceWindow();
  return (typeof CAMPAIGN_WINDOW_LABELS !== "undefined" && CAMPAIGN_WINDOW_LABELS[w]) || w;
}

// ── Entry point: the Keywords tab of Keywords & Search Terms (PR-ADS-177C) ───
// The Keyword Evidence page is retired as a page (#/keywords redirects here);
// its module renders unchanged into the shared tab body.
function loadKeywordsTab() {
  const body = document.getElementById("st-tab-body");
  if (!body || _stTab !== "keywords") return;
  _kwFilters.page = 1;
  body.innerHTML = `<div id="kw-tab-body"></div>`;
  loadKeywordHistory();
  loadKeywordTab();
}

// Fetch all-time completeness metadata (PR-ADS-146A §6). Non-blocking — the tab
// renders from /api/keyword-evidence; this only refines the coverage note once
// it resolves. Never calls Google Ads (reads PostgreSQL bootstrap state only).
async function loadKeywordHistory() {
  try {
    const data = await fetchJSON("/api/keyword-evidence/history");
    _kwHistory = data && data.available ? data : null;
  } catch (err) {
    _kwHistory = null;
  }
  // Re-render if the tab has already painted, so the note reflects completeness.
  if (_kwData && document.getElementById("kw-tab-body")) renderKeywordTab();
}

function kwBuildParams() {
  const p = new URLSearchParams();
  p.set("window", getEvidenceWindow());
  p.set("page", String(_kwFilters.page));
  p.set("page_size", "50");
  p.set("sort", _kwFilters.sort || "spend");
  if (_kwFilters.q) p.set("q", _kwFilters.q);
  if (_kwFilters.campaign) p.set("campaign", _kwFilters.campaign);
  if (_kwFilters.matchType) p.set("match_type", _kwFilters.matchType);
  if (_kwFilters.status) p.set("criterion_status", _kwFilters.status);
  if (_kwFilters.quality) p.set("quality_band", _kwFilters.quality);
  if (_kwFilters.signal) p.set("signal", _kwFilters.signal);
  if (_kwFilters.sqlState) p.set("sql_state", _kwFilters.sqlState);
  if (_kwFilters.minSpend !== "" && _kwFilters.minSpend !== null) p.set("min_spend", String(_kwFilters.minSpend));
  return p;
}

async function loadKeywordTab() {
  const reqId = ++_kwReqId;
  _kwLoadState = "loading";
  renderKeywordTab();
  try {
    const data = await fetchJSON(`/api/keyword-evidence?${kwBuildParams().toString()}`);
    if (reqId !== _kwReqId) return;
    _kwData = data;
    _kwLoadState = data.db_unavailable ? "db_unavailable" : "ok";
  } catch (err) {
    if (reqId !== _kwReqId) return;
    console.error("keyword-evidence load failed", err);
    _kwLoadState = "error";
  }
  renderKeywordTab();
}

function kwHasActiveFilters() {
  return Boolean(_kwFilters.q || _kwFilters.campaign || _kwFilters.matchType ||
    _kwFilters.status || _kwFilters.quality || _kwFilters.signal || _kwFilters.minSpend !== "");
}

// Compact disclosure when the selected window reaches before the stored durable
// range (PR-ADS-146 §5) — never implies 180d / All time is fully covered.
function kwHistoricalCoverageNote() {
  if (!_kwData) return "";
  const ds = _kwData.durable_coverage_start;
  const de = _kwData.durable_coverage_end;
  const ws = _kwData.window_start;
  if (!ds) return "";
  // §6 — when the full account history is backfilled, All-time is complete and
  // carries no partial-coverage warning. history_complete is authoritative
  // (bootstrap start == verified account-history start AND no missing ranges).
  const historyComplete = !!(_kwHistory && _kwHistory.history_complete);
  const reachesBeforeCoverage = _kwData.all_time || (ws && ws < ds);
  if (!reachesBeforeCoverage) return "";
  if (_kwData.all_time && historyComplete) return "";
  return `<div class="kw-coverage-note" role="note">
    <strong>Partial historical coverage.</strong> Durable keyword facts are stored from
    ${escapeHtml(ds)} to ${escapeHtml(de || ds)}; earlier dates in this window are not yet backfilled.</div>`;
}

function renderKeywordTab() {
  const body = document.getElementById("kw-tab-body");
  if (!body) return;
  if (_kwLoadState === "loading" && !_kwData) {
    body.innerHTML = `<div class="evidence-empty-state">Loading keyword evidence…</div>`;
    return;
  }
  if (_kwLoadState === "db_unavailable") {
    body.innerHTML = `<div class="evidence-empty-state">
      Data unavailable — the keyword source cannot be reached right now.
      No metrics are shown because none can be verified.
      <a href="#/health">Check System Status</a>.</div>`;
    return;
  }
  if (_kwLoadState === "error") {
    body.innerHTML = `<div class="evidence-empty-state">
      Keyword evidence could not be loaded. Retry, or check System Status.</div>`;
    return;
  }
  if (!_kwData) return;

  const total = (_kwData.pagination && _kwData.pagination.total_count) || 0;
  if (total === 0 && !kwHasActiveFilters()) {
    // §8 — distinguish "not run / running / failed" from a genuinely verified
    // empty window. Only the latter may present zero as evidence.
    const st = kwFirstSyncState();
    let msg;
    if (st === "not_run") {
      msg = "Keyword facts have not completed their first sync. Historical backfill runs automatically after deploy — check back once it finishes.";
    } else if (st === "running") {
      msg = "Keyword facts are syncing for the first time. Metrics will appear once the initial sync completes.";
    } else if (st === "failed_no_data") {
      msg = "The latest keyword sync failed and no verified data is available yet. Check System Status.";
    } else if (st === "db_unavailable") {
      msg = "Data unavailable — the keyword source cannot be reached right now.";
    } else if (!kwWindowProvenSynced()) {
      msg = "This window predates completed keyword facts and has not been backfilled yet.";
    } else {
      msg = "No reported keyword activity for this window.";
    }
    body.innerHTML = `${kwHistoricalCoverageNote()}${renderKeywordKPIs(_kwData.kpis)}
      <div class="evidence-empty-state">${escapeHtml(msg)}</div>`;
    return;
  }

  body.innerHTML = `
    ${kwHistoricalCoverageNote()}
    ${renderKeywordKPIs(_kwData.kpis)}
    ${renderMatchTypeSummary(_kwData.match_type_summary)}
    ${renderKeywordFilters(_kwData.facets)}
    ${kwSqlCoverageNote()}
    <div class="evidence-table-shell">
      <div class="evidence-table-scroll" id="kw-table"></div>
    </div>`;
  renderKeywordTable();
  wireKeywordControls();
}

// §9 — compact SQL-attribution coverage disclosure (window-scoped). Never lets
// low attribution coverage hide behind a headline count.
function kwSqlCoverageNote() {
  const s = _kwData && _kwData.sql_attribution;
  const r = _kwData && _kwData.sql_reconciliation;
  if (!s) return "";
  // PR-ADS-152: a canonical-scope mismatch is never rendered as a normal count.
  if (r && r.reconciliation_status === "mismatch") {
    return `<div class="kw-sql-note" role="note">SQL attribution coverage: <strong>Reconciliation required</strong> — the canonical SQL scopes do not reconcile for this window, so the counts are withheld.</div>`;
  }
  if (!s.sql_attribution_available) {
    return `<div class="kw-sql-note" role="note">SQL attribution unavailable — HubSpot qualified-contact evidence could not be loaded for this window.</div>`;
  }
  // The nested scopes, most-inclusive first: Google Ads-source ⊇
  // campaign-attributable ⊇ uniquely keyword-attributed.
  const gaSource = r ? r.google_ads_source_sqls : null;
  const campAttr = r ? r.campaign_attributable_sqls : null;
  const uniq = s.sql_attributed_count;
  const amb = (s.sql_ambiguous_count || 0) + (s.sql_unattributed_count || 0);
  return `<div class="kw-sql-note" role="note">
    <div class="kw-sql-coverage-title">SQL attribution coverage:</div>
    <ul class="kw-sql-coverage">
      <li>Google Ads-source SQLs: <strong>${dashValue(gaSource, fmtCount)}</strong></li>
      <li>Campaign-attributable SQLs: <strong>${dashValue(campAttr, fmtCount)}</strong></li>
      <li>Uniquely keyword-attributed SQLs: <strong>${dashValue(uniq, fmtCount)}</strong></li>
      <li>Ambiguous/unattributed: <strong>${fmtCount(amb)}</strong></li>
    </ul>
    <div class="kw-sql-coverage-foot">Each scope is a subset of the one above it. SQLs are HubSpot-confirmed qualified contacts on their created date — not Google Ads conversions.</div>
  </div>`;
}

// §7 — one compact Attributed-SQLs cell. Positive = restrained green; genuine
// zero = neutral; ambiguous / unavailable = "—" with an explanatory tooltip.
function kwSqlCell(r) {
  const st = r.sql_attribution_status;
  const n = r.attributed_sqls;
  // Confirmed positive attribution.
  if (st === "attributed" && n > 0) {
    return `<span class="kw-sql kw-sql--pos" title="Confirmed attribution: ${escapeHtml(String(n))} HubSpot-confirmed SQL contact(s) uniquely tied to this keyword. This is a confirmed attributed count, not the complete SQL total.">${fmtCount(n)}</span>`;
  }
  // Proven zero — only when the window's SQL population is complete enough to prove it.
  if (st === "known_zero") {
    return `<span class="kw-sql kw-sql--zero" title="Proven zero: attribution coverage is complete for this window and no qualified contact maps to this uniquely-attributable keyword">0</span>`;
  }
  if (st === "ambiguous") {
    const reason = r.sql_ambiguity_reason || "Keyword text matches multiple criteria — SQL not uniquely attributable";
    return `<span class="kw-sql kw-sql--na" title="${escapeHtml(reason)}">—</span>`;
  }
  if (st === "partial_attribution") {
    const reason = r.sql_ambiguity_reason || "Unavailable — attribution coverage is incomplete this window (unresolved-campaign or missing-keyword contacts), so a zero cannot be proven";
    return `<span class="kw-sql kw-sql--na" title="${escapeHtml(reason)}">—</span>`;
  }
  if (st === "mapping_review") {
    return `<span class="kw-sql kw-sql--na" title="Campaign identity unresolved (mapping review) — SQL attribution withheld">—</span>`;
  }
  return `<span class="kw-sql kw-sql--na" title="SQL attribution unavailable for this window">—</span>`;
}

// Truthful first-sync state for the durable keyword-facts dataset
// (PR-ADS-146A §8). Reads the canonical freshness verdict for
// google_ads_api/keyword_facts and falls back to the durable coverage
// reported in the evidence payload when the freshness cache is cold.
// Returns one of: db_unavailable | not_run | running | failed_no_data |
// has_data.
function kwFirstSyncState() {
  const row = _datasetFreshnessByKey && _datasetFreshnessByKey["google_ads_api/keyword_facts"];
  const status = row && row.canonical_status;
  const hasDurable = !!(_kwData && _kwData.durable_coverage_start);
  if (status === "db_unavailable") return "db_unavailable";
  if (status === "running") return "running";
  if (status === "not_run" || status === "not_run_no_upstream_data") {
    // Freshness says never-run; trust durable facts if the payload proves any
    // exist (covers a freshness cache that lags a completed backfill).
    return hasDurable ? "has_data" : "not_run";
  }
  if (status === "failed_no_data") return hasDurable ? "has_data" : "failed_no_data";
  if (!status) return hasDurable ? "has_data" : "not_run";
  return "has_data";
}

// Whether the SELECTED window was actually synced (PR-ADS-146A §6/§8). The
// backend proves this (window start covered, end within the sync watermark, no
// missing range); a verified-empty window may present $0.00 only when true.
function kwWindowProvenSynced() {
  if (!_kwData) return false;
  // Authoritative backend verdict when present.
  if (typeof _kwData.selected_window_fully_synced === "boolean") {
    return _kwData.selected_window_fully_synced;
  }
  // Conservative fallback for older payloads without the coverage proof.
  const ds = _kwData.durable_coverage_start;
  if (!ds) return false;
  if (_kwData.all_time) return false;
  const ws = _kwData.window_start;
  if (ws && ws < ds) return false;
  return true;
}

// ── KPI strip (5 compact cards + coverage) ───────────────────────────────────
function kwVerifiedSpendCard(kpis) {
  // §8 — before the first successful keyword-fact sync, never render zero spend
  // as verified evidence. Distinguish not-run / running / failed-no-data.
  const syncState = kwFirstSyncState();
  if (syncState === "not_run" || syncState === "db_unavailable") {
    return stKpiCard("Verified Keyword Spend", stUnavailable(),
      "Keyword facts have not completed their first sync");
  }
  if (syncState === "running") {
    return stKpiCard("Verified Keyword Spend", stUnavailable(),
      "Keyword facts are syncing for the first time");
  }
  if (syncState === "failed_no_data") {
    return stKpiCard("Verified Keyword Spend", stUnavailable(),
      "Latest keyword sync failed — no verified data available yet");
  }
  const mon = kpis.monetary || {};
  const status = kpis.monetary_status || mon.monetary_completeness_status || "unavailable";
  const usd = kpis.verified_spend_usd;
  if (status === "unavailable" || usd === null || usd === undefined) {
    return stKpiCard("Verified Keyword Spend", stUnavailable(),
      "No verified currency lineage for this window");
  }
  // §8 — a zero verified subtotal is truthful "Complete" only when the window is
  // proven synced. Otherwise it predates the backfill and zero is not evidence.
  if (Number(usd) === 0 && !kwWindowProvenSynced()) {
    return stKpiCard("Verified Keyword Spend", stUnavailable(),
      "This window predates completed keyword facts — not yet backfilled");
  }
  const nativeSub = (mon.verified_native_spend !== null && mon.verified_native_spend !== undefined)
    ? `${escapeHtml(mon.native_currency || "GBP")} ${Number(mon.verified_native_spend).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} native`
    : "";
  const badge = status === "partial"
    ? `<span class="st-mon-badge st-mon-badge--partial" title="Verified subtotal — excludes unverified or FX-incomplete keyword units">Partial</span>`
    : `<span class="st-mon-badge st-mon-badge--complete" title="All keyword units verified and FX-complete">Complete</span>`;
  let sub;
  if (status === "partial") {
    sub = `${badge} ${kwExclusionPhrase(mon.unverified_currency_units, mon.fx_incomplete_units)}` +
      (nativeSub ? ` · ${nativeSub}` : "");
  } else {
    sub = `${badge} ${nativeSub ? `${nativeSub} · FX-converted USD` : "FX-converted USD"}`;
  }
  return stKpiCard("Verified Keyword Spend", stMoney(usd), sub);
}

function kwExclusionPhrase(legacy, fxIncomplete) {
  legacy = legacy || 0; fxIncomplete = fxIncomplete || 0;
  const total = legacy + fxIncomplete;
  if (!total) return "excludes unverified keyword units";
  if (legacy && fxIncomplete) {
    return `excludes ${fmtCount(total)} unverified units (${fmtCount(legacy)} legacy · ${fmtCount(fxIncomplete)} FX-incomplete)`;
  }
  if (fxIncomplete) return `excludes ${fmtCount(fxIncomplete)} FX-incomplete unit${fxIncomplete === 1 ? "" : "s"}`;
  return `excludes ${fmtCount(legacy)} unverified keyword unit${legacy === 1 ? "" : "s"}`;
}

function renderKeywordKPIs(kpis) {
  if (!kpis) return "";
  const broad = kpis.broad_match_exposure || {};
  const qual = kpis.quality_evidence || {};
  const coverage = kpis.coverage || {};
  const broadVal = (broad.broad_verified_spend_usd === null || broad.broad_verified_spend_usd === undefined)
    ? stUnavailable()
    : stMoney(broad.broad_verified_spend_usd);
  const broadSub = (broad.broad_share_pct !== null && broad.broad_share_pct !== undefined)
    ? `${broad.broad_share_pct.toFixed(0)}% of verified keyword spend`
    : "Broad verified spend";
  const qualVal = (qual.with_quality === null || qual.with_quality === undefined)
    ? stUnavailable()
    : `${fmtCount(qual.with_quality)} of ${fmtCount(qual.active_criteria)}`;
  const cards = [
    stKpiCard("Keywords Reported", kwNum(kpis.keywords_reported), "Unique keyword criteria in window"),
    kwVerifiedSpendCard(kpis),
    stKpiCard("Clicks", kwNum(kpis.clicks), ""),
    stKpiCard("Broad Match Exposure", broadVal, broadSub),
    stKpiCard("Keywords with Quality Evidence", qualVal, "Latest observed Google Ads quality diagnostics"),
  ];
  if (coverage.status === "ok" && coverage.coverage_pct !== null &&
      coverage.coverage_pct !== undefined && !kwHasActiveFilters()) {
    const verifiedOnly = coverage.scope === "verified_only";
    cards.push(stKpiCard(
      "Keyword-view Coverage",
      `${Number(coverage.coverage_pct).toFixed(1)}%`,
      (verifiedOnly
        ? `Verified keyword-view rows only — ${kwExclusionPhrase(coverage.excluded_unverified_units, coverage.excluded_fx_incomplete_units)} · of `
        : "Keyword-view reporting coverage of ") +
      `${stMoney(coverage.canonical_spend_usd)} canonical spend`));
  }
  return `<div class="evidence-kpi-grid st-kpi-grid">${cards.join("")}</div>`;
}

// ── Compact match-type summary (slim comparison row) ─────────────────────────
function renderMatchTypeSummary(summary) {
  if (!summary || !summary.buckets || !summary.buckets.length) return "";
  const cells = summary.buckets.map((b) => `
    <div class="kw-mt-seg">
      <div class="kw-mt-seg__head">${kwMatchBadge(b.match_type)}
        <span class="kw-mt-seg__kw">${fmtCount(b.unique_keywords)} kw</span></div>
      <div class="kw-mt-seg__spend">${stMoney(b.verified_spend_usd)}${b.spend_partial ? ` <span class="st-mon-badge st-mon-badge--partial" title="Verified subtotal — some units unverified">Partial</span>` : ""}</div>
      <div class="kw-mt-seg__meta">${b.spend_share_pct !== null && b.spend_share_pct !== undefined ? `${b.spend_share_pct.toFixed(0)}% share` : "—"} · ${fmtCount(b.clicks)} clicks · CTR ${kwPct(b.ctr)} · CPC ${b.avg_cpc_usd !== null && b.avg_cpc_usd !== undefined ? stMoney(b.avg_cpc_usd) : "—"} · ${b.platform_conversions} conv</div>
    </div>`).join("");
  return `<div class="kw-mt-summary" aria-label="Match-type exposure (verified spend)">${cells}</div>`;
}

// ── Filter row ───────────────────────────────────────────────────────────────
function kwCampaignOptions(facets, selected) {
  const opts = [`<option value="">All campaigns</option>`];
  (facets && facets.campaigns ? facets.campaigns : []).forEach((c) => {
    const marker = c.mapping_status === "unmatched" ? " (mapping review)"
      : c.mapping_status === "not_google_ads" ? " (not Google Ads)" : "";
    opts.push(`<option value="${escapeHtml(c.campaign_key)}"${c.campaign_key === selected ? " selected" : ""}>${escapeHtml(c.campaign_name || c.campaign_key)}${marker}</option>`);
  });
  return opts.join("");
}

function renderKeywordFilters(facets) {
  const sigOpts = [`<option value="">All review signals</option>`]
    .concat((facets && facets.review_signals ? facets.review_signals : []).map((s) =>
      `<option value="${escapeHtml(s)}"${s === _kwFilters.signal ? " selected" : ""}>${escapeHtml(s)}</option>`)).join("");
  return `<div class="campaign-controls st-controls">
    <input type="search" id="kw-q" class="waste-search-input" placeholder="Search keyword, campaign or ad group…"
      value="${escapeHtml(_kwFilters.q)}" aria-label="Search keywords" />
    <select id="kw-campaign" class="waste-filter-select" aria-label="Filter by campaign">${kwCampaignOptions(facets, _kwFilters.campaign)}</select>
    <select id="kw-matchtype" class="waste-filter-select" aria-label="Filter by match type">
      <option value="">All match types</option>
      <option value="BROAD"${_kwFilters.matchType === "BROAD" ? " selected" : ""}>Broad</option>
      <option value="PHRASE"${_kwFilters.matchType === "PHRASE" ? " selected" : ""}>Phrase</option>
      <option value="EXACT"${_kwFilters.matchType === "EXACT" ? " selected" : ""}>Exact</option>
      <option value="UNKNOWN"${_kwFilters.matchType === "UNKNOWN" ? " selected" : ""}>Unknown</option>
    </select>
    <select id="kw-status" class="waste-filter-select" aria-label="Filter by criterion status">
      <option value="">All statuses</option>
      <option value="ENABLED"${_kwFilters.status === "ENABLED" ? " selected" : ""}>Enabled</option>
      <option value="PAUSED"${_kwFilters.status === "PAUSED" ? " selected" : ""}>Paused</option>
      <option value="REMOVED"${_kwFilters.status === "REMOVED" ? " selected" : ""}>Removed</option>
    </select>
    <select id="kw-quality" class="waste-filter-select" aria-label="Filter by quality band">
      <option value="">All quality</option>
      <option value="strong"${_kwFilters.quality === "strong" ? " selected" : ""}>Strong</option>
      <option value="medium"${_kwFilters.quality === "medium" ? " selected" : ""}>Medium</option>
      <option value="weak"${_kwFilters.quality === "weak" ? " selected" : ""}>Weak</option>
      <option value="unavailable"${_kwFilters.quality === "unavailable" ? " selected" : ""}>Unavailable</option>
    </select>
    <select id="kw-signal" class="waste-filter-select" aria-label="Filter by review signal">${sigOpts}</select>
    <select id="kw-sqlstate" class="waste-filter-select" aria-label="Filter by SQL attribution">
      <option value=""${!_kwFilters.sqlState ? " selected" : ""}>All SQL attribution</option>
      <option value="has_sql"${_kwFilters.sqlState === "has_sql" ? " selected" : ""}>Has attributed SQL</option>
      <option value="known_zero"${_kwFilters.sqlState === "known_zero" ? " selected" : ""}>Known zero</option>
      <option value="ambiguous"${_kwFilters.sqlState === "ambiguous" ? " selected" : ""}>Ambiguous</option>
      <option value="unavailable"${_kwFilters.sqlState === "unavailable" ? " selected" : ""}>Unavailable</option>
    </select>
    <input type="number" id="kw-min-spend" min="0" step="1" class="waste-search-input st-min-spend"
      placeholder="Min $" value="${escapeHtml(String(_kwFilters.minSpend))}" aria-label="Minimum verified spend" />
    <select id="kw-sort" class="waste-filter-select" aria-label="Sort">
      <option value="spend"${_kwFilters.sort === "spend" ? " selected" : ""}>Highest spend</option>
      <option value="clicks"${_kwFilters.sort === "clicks" ? " selected" : ""}>Most clicks</option>
      <option value="cpc"${_kwFilters.sort === "cpc" ? " selected" : ""}>Highest CPC</option>
      <option value="ctr"${_kwFilters.sort === "ctr" ? " selected" : ""}>Highest CTR</option>
      <option value="quality"${_kwFilters.sort === "quality" ? " selected" : ""}>Quality score</option>
      <option value="attributed_sqls"${_kwFilters.sort === "attributed_sqls" ? " selected" : ""}>Highest attributed SQLs</option>
      <option value="keyword"${_kwFilters.sort === "keyword" ? " selected" : ""}>Keyword A–Z</option>
      <option value="last_seen"${_kwFilters.sort === "last_seen" ? " selected" : ""}>Recently active</option>
    </select>
    <button id="kw-reset" class="btn btn--secondary" type="button">Reset</button>
    <button id="kw-export" class="btn btn--secondary" type="button">Export CSV</button>
  </div>`;
}

// ── Table ────────────────────────────────────────────────────────────────────
function kwCampaignCell(r) {
  const marker = r.mapping_status === "unmatched"
    ? `<span class="st-map-note" title="Unmatched — mapping review">Mapping review</span>`
    : r.mapping_status === "not_google_ads"
    ? `<span class="st-map-note">Not Google Ads</span>` : "";
  return `${escapeHtml(r.campaign || "—")}${marker ? `<br>${marker}` : ""}`;
}

function renderKeywordTable() {
  const host = document.getElementById("kw-table");
  if (!host || !_kwData) return;
  const rows = _kwData.rows || [];
  const pg = _kwData.pagination || {};
  if (!rows.length) {
    host.innerHTML = `<div class="evidence-empty-state">No keywords match the current filters.</div>`;
    return;
  }
  const bodyRows = rows.map((r) => `
    <tr class="kw-row" data-kw-key="${escapeHtml(r.criterion_key)}" tabindex="0" role="button" aria-label="Open keyword evidence for ${escapeHtml(r.keyword || "keyword")}">
      <td class="st-td-term" data-label="Keyword">${escapeHtml(r.keyword || "—")}</td>
      <td data-label="Match">${kwMatchBadge(r.match_type)}</td>
      <td class="st-td-campaign" data-label="Campaign">${kwCampaignCell(r)}</td>
      <td data-label="Ad Group">${escapeHtml(r.ad_group || "—")}</td>
      <td class="num" data-label="Spend">${kwSpendCell(r)}</td>
      <td class="num" data-label="Clicks">${fmtCount(r.clicks)}</td>
      <td class="num" data-label="CTR">${kwPct(r.ctr)}</td>
      <td class="num" data-label="CPC">${r.cpc_usd !== null && r.cpc_usd !== undefined ? stMoney(r.cpc_usd) : "—"}</td>
      <td class="num" data-label="Quality">${kwQualityCell(r)}</td>
      <td class="num" data-label="Attributed SQLs">${kwSqlCell(r)}</td>
      <td data-label="Signal">${kwSignalBadge(r.review_signal)}</td>
      <td class="st-td-chev" aria-hidden="true">›</td>
    </tr>`).join("");
  const start = ((pg.page || 1) - 1) * (pg.page_size || 50);
  const shown = `${rows.length ? start + 1 : 0}–${start + rows.length}`;
  host.innerHTML = `
    <table class="data-table campaign-decision-table kw-table">
      <thead><tr>
        <th>Keyword</th><th>Match</th><th>Campaign</th><th>Ad Group</th>
        <th class="num">Spend</th><th class="num">Clicks</th><th class="num">CTR</th>
        <th class="num">CPC</th><th class="num">Quality</th>
        <th class="num">Attributed SQLs</th><th>Signal</th><th></th>
      </tr></thead>
      <tbody>${bodyRows}</tbody>
    </table>
    <div class="st-pagination">
      <span class="st-pagination__info">Showing ${shown} of ${fmtCount(pg.total_count || 0)} keywords</span>
      <span class="st-pagination__btns">
        <button id="kw-prev" type="button" ${(pg.page || 1) <= 1 ? "disabled" : ""}>‹ Prev</button>
        <button id="kw-next" type="button" ${pg.has_more ? "" : "disabled"}>Next ›</button>
      </span>
    </div>`;
  host.querySelectorAll(".kw-row").forEach((tr) => {
    const open = () => openKwDrawer(tr.dataset.kwKey);
    tr.addEventListener("click", open);
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
  });
  const prev = document.getElementById("kw-prev");
  const next = document.getElementById("kw-next");
  if (prev) prev.addEventListener("click", () => { if (_kwFilters.page > 1) { _kwFilters.page--; loadKeywordTab(); } });
  if (next) next.addEventListener("click", () => { if (pg.has_more) { _kwFilters.page++; loadKeywordTab(); } });
}

// ── Controls wiring + CSV export ─────────────────────────────────────────────
function wireKeywordControls() {
  const applyText = () => { _kwFilters.page = 1; loadKeywordTab(); };
  const q = document.getElementById("kw-q");
  if (q) q.addEventListener("input", () => {
    _kwFilters.q = q.value.trim();
    clearTimeout(_kwDebounce);
    _kwDebounce = setTimeout(applyText, 300);
  });
  const bind = (id, key, transform) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("change", () => { _kwFilters[key] = transform ? transform(el.value) : el.value; applyText(); });
  };
  bind("kw-campaign", "campaign");
  bind("kw-matchtype", "matchType");
  bind("kw-status", "status");
  bind("kw-quality", "quality");
  bind("kw-signal", "signal");
  bind("kw-sqlstate", "sqlState");
  bind("kw-sort", "sort");
  const ms = document.getElementById("kw-min-spend");
  if (ms) ms.addEventListener("change", () => { _kwFilters.minSpend = ms.value === "" ? "" : Number(ms.value); applyText(); });
  const reset = document.getElementById("kw-reset");
  if (reset) reset.addEventListener("click", () => {
    _kwFilters = { q: "", campaign: "", matchType: "", status: "", quality: "", signal: "", sqlState: "", minSpend: "", sort: "spend", page: 1 };
    loadKeywordTab();
  });
  const exp = document.getElementById("kw-export");
  if (exp) exp.addEventListener("click", () => {
    const p = kwBuildParams();
    p.delete("page"); p.delete("page_size");
    window.location.href = `/api/keyword-evidence/export?${p.toString()}`;
  });
}

// ── Drawer ───────────────────────────────────────────────────────────────────
function kwDrawerKpi(label, valueHtml) {
  return `<div class="st-drawer-kpi"><div class="st-drawer-kpi__label">${label}</div><div class="st-drawer-kpi__value">${valueHtml}</div></div>`;
}

function closeKwDrawer() {
  const overlay = document.getElementById("kw-drawer-overlay");
  const drawer = document.getElementById("kw-drawer");
  if (overlay) { overlay.hidden = true; overlay.setAttribute("aria-hidden", "true"); }
  if (drawer) { drawer.hidden = true; }
  if (_kwDrawerKeyHandler) { document.removeEventListener("keydown", _kwDrawerKeyHandler); _kwDrawerKeyHandler = null; }
}

async function openKwDrawer(criterionKey) {
  const overlay = document.getElementById("kw-drawer-overlay");
  const drawer = document.getElementById("kw-drawer");
  const title = document.getElementById("kw-drawer-title");
  const bodyEl = document.getElementById("kw-drawer-body");
  if (!overlay || !drawer || !bodyEl) return;
  overlay.hidden = false; overlay.setAttribute("aria-hidden", "false");
  drawer.hidden = false;
  if (title) title.textContent = "Keyword evidence";
  bodyEl.innerHTML = `<p class="empty-state">Loading…</p>`;
  overlay.onclick = closeKwDrawer;
  const closeBtn = document.getElementById("kw-drawer-close-btn");
  if (closeBtn) closeBtn.onclick = closeKwDrawer;
  _kwDrawerKeyHandler = (e) => { if (e.key === "Escape") closeKwDrawer(); };
  document.addEventListener("keydown", _kwDrawerKeyHandler);
  try {
    const p = new URLSearchParams({ criterion_key: criterionKey, window: getEvidenceWindow() });
    const data = await fetchJSON(`/api/keyword-evidence/detail?${p.toString()}`);
    renderKwDrawer(bodyEl, data);
  } catch (err) {
    console.error("keyword drawer load failed", err);
    bodyEl.innerHTML = `<p class="empty-state">Evidence could not be loaded.</p>`;
  }
}

function renderKwDrawer(bodyEl, data) {
  if (!data || data.found === false || !data.keyword) {
    bodyEl.innerHTML = `<p class="empty-state">No evidence found for this keyword in the selected window.</p>`;
    return;
  }
  const k = data.keyword;
  const id = data.identity || {};
  const act = data.activity || {};
  const lq = data.latest_quality || {};
  const daily = data.daily || [];
  const title = document.getElementById("kw-drawer-title");
  if (title) title.textContent = k.keyword || "Keyword evidence";

  const dailyRows = daily.map((d) => `<tr>
    <td>${escapeHtml(d.source_date || "—")}</td>
    <td class="num">${d.spend_usd !== null && d.spend_usd !== undefined ? stMoney(d.spend_usd) : (d.spend_native !== null && d.spend_native !== undefined ? `${escapeHtml(d.native_currency || "?")} ${Number(d.spend_native).toFixed(2)}` : "—")}</td>
    <td class="num">${fmtCount(d.clicks || 0)}</td>
    <td class="num">${fmtCount(d.impressions || 0)}</td>
    <td class="num">${d.impressions ? ((d.clicks || 0) / d.impressions * 100).toFixed(2) + "%" : "—"}</td>
  </tr>`).join("");

  bodyEl.innerHTML = `
    <div class="st-drawer-section">
      <div class="st-drawer-badges">${kwMatchBadge(k.match_type)} ${kwSignalBadge(data.review_signal)}
        ${id.criterion_status ? `<span class="kw-status-pill">${escapeHtml(id.criterion_status)}</span>` : ""}</div>
      <dl class="st-drawer-facts">
        <dt>Campaign</dt><dd>${escapeHtml(id.campaign || "—")}${id.mapping_status === "unmatched" ? " (mapping review)" : ""}</dd>
        <dt>Campaign ID</dt><dd>${escapeHtml(id.campaign_id || "—")}</dd>
        <dt>Ad group</dt><dd>${escapeHtml(id.ad_group || "—")}</dd>
        <dt>Ad group ID</dt><dd>${escapeHtml(id.ad_group_id || "—")}</dd>
        <dt>Criterion ID</dt><dd>${escapeHtml(id.criterion_id || "—")}</dd>
      </dl>
    </div>
    <div class="st-drawer-section">
      <h4 class="st-drawer-h">Selected-window activity</h4>
      <div class="st-drawer-kpis">
        ${kwDrawerKpi("Verified spend", act.spend_usd !== null && act.spend_usd !== undefined ? stMoney(act.spend_usd) : stUnavailable())}
        ${kwDrawerKpi("Native", act.spend_native !== null && act.spend_native !== undefined ? `${escapeHtml(act.native_currency || "?")} ${Number(act.spend_native).toFixed(2)}` : "—")}
        ${kwDrawerKpi("Clicks", fmtCount(act.clicks || 0))}
        ${kwDrawerKpi("Impressions", fmtCount(act.impressions || 0))}
        ${kwDrawerKpi("CTR", act.ctr !== null && act.ctr !== undefined ? act.ctr.toFixed(2) + "%" : "—")}
        ${kwDrawerKpi("CPC", act.cpc_usd !== null && act.cpc_usd !== undefined ? stMoney(act.cpc_usd) : "—")}
      </div>
      <p class="st-drawer-note">Platform conversions: ${(act.platform_conversions === null || act.platform_conversions === undefined) ? "Unavailable" : act.platform_conversions}. ${escapeHtml(act.platform_conversion_disclosure || "")}</p>
      <p class="st-drawer-note">Reported ${escapeHtml(act.first_seen || "—")} → ${escapeHtml(act.last_seen || "—")}. Currency: ${escapeHtml(act.currency_status || "—")}.</p>
    </div>
    <div class="st-drawer-section">
      <h4 class="st-drawer-h">Latest observed quality diagnostics</h4>
      <dl class="st-drawer-facts">
        <dt>Quality Score</dt><dd>${lq.quality_score === null || lq.quality_score === undefined ? "Unavailable" : lq.quality_score}</dd>
        <dt>Expected CTR</dt><dd>${escapeHtml(lq.expected_ctr || "Unavailable")}</dd>
        <dt>Ad Relevance</dt><dd>${escapeHtml(lq.ad_relevance || "Unavailable")}</dd>
        <dt>Landing Page Exp.</dt><dd>${escapeHtml(lq.landing_page_experience || "Unavailable")}</dd>
        <dt>Observed</dt><dd>${escapeHtml(lq.observed_date || "—")}</dd>
      </dl>
      <p class="st-drawer-note">${escapeHtml(lq.semantics || "")}</p>
    </div>
    ${kwDrawerSqlSection(data.sql_attribution)}
    <div class="st-drawer-section">
      <h4 class="st-drawer-h">Daily evidence</h4>
      ${daily.length ? `<table class="st-fact-table"><thead><tr><th>Date</th><th class="num">Spend</th><th class="num">Clicks</th><th class="num">Impr.</th><th class="num">CTR</th></tr></thead><tbody>${dailyRows}</tbody></table>` : `<p class="st-drawer-note">No reported daily rows in this window.</p>`}
    </div>
    <div class="st-drawer-links">
      <a href="#/search-terms?campaign=${encodeURIComponent(id.campaign_key || "")}">View related search terms →</a>
    </div>`;
}

// §7 — HubSpot SQL attribution section for the keyword drawer. Shows the state,
// coverage and the supporting deduplicated qualified contacts (never emails).
function kwDrawerSqlSection(s) {
  if (!s) return "";
  const statusLabel = {
    attributed: "Attributed", known_zero: "Known zero",
    ambiguous: "Ambiguous", mapping_review: "Mapping review",
    unavailable: "Unavailable",
  }[s.sql_attribution_status] || "Unavailable";
  const countHtml = (s.attributed_sqls === null || s.attributed_sqls === undefined)
    ? "—"
    : `<span class="kw-sql ${s.attributed_sqls > 0 ? "kw-sql--pos" : "kw-sql--zero"}">${fmtCount(s.attributed_sqls)}</span>`;
  const cov = (s.sql_attribution_coverage_pct === null || s.sql_attribution_coverage_pct === undefined)
    ? "—" : `${s.sql_attribution_coverage_pct}%`;
  const contacts = s.contacts || [];
  const rows = contacts.map((c) => `<tr>
    <td>${escapeHtml(c.company || "—")}</td>
    <td>${escapeHtml(c.contact_id || "—")}</td>
    <td>${escapeHtml(c.sql_date || "—")}</td>
    <td>${escapeHtml(c.campaign_label || "—")}</td>
    <td>${escapeHtml(c.keyword || "—")}</td>
    <td>${c.has_gclid ? "Yes" : "No"}</td>
  </tr>`).join("");
  return `
    <div class="st-drawer-section">
      <h4 class="st-drawer-h">HubSpot SQL attribution</h4>
      <div class="st-drawer-kpis">
        ${kwDrawerKpi("Attributed SQLs", countHtml)}
        ${kwDrawerKpi("Status", escapeHtml(statusLabel))}
        ${kwDrawerKpi("Window coverage", cov)}
      </div>
      ${s.sql_ambiguity_reason ? `<p class="st-drawer-note">${escapeHtml(s.sql_ambiguity_reason)}</p>` : ""}
      <p class="st-drawer-note">Source: HubSpot-confirmed qualified contacts on their created date (contact_created_at). A platform conversion is never counted as an SQL.</p>
      ${contacts.length
        ? `<table class="st-fact-table"><thead><tr><th>Company</th><th>Contact ID</th><th>SQL date</th><th>Campaign</th><th>Keyword</th><th>GCLID</th></tr></thead><tbody>${rows}</tbody></table>`
        : `<p class="st-drawer-note">No supporting qualified contacts for this keyword in the selected window.</p>`}
    </div>`;
}

// PR-ADS-153C: the In Progress Leads page is retired. Its concept is now the
// `open_working` operational filter on the canonical Leads page, driven by
// mql_status_category (which maps every OPEN - * status, including Connecting).


// ── Scheduler page ─────────────────────────────────────────────────────────

async function loadScheduler() {
  renderRunMeta("scheduler");
  const gridEl = document.getElementById("sched-jobs-grid");
  if (gridEl) gridEl.innerHTML = `<p class="empty-state">Loading scheduler…</p>`;

  try {
    const data = await fetchJSON("/scheduler/status");
    const jobs = Array.isArray(data.jobs) ? data.jobs : [];

    if (jobs.length === 0) {
      if (gridEl) gridEl.innerHTML = `<p class="empty-state">No scheduler jobs found.</p>`;
      return;
    }

    const isAdmin = _currentUser && _currentUser.role === "admin";

    if (gridEl) {
      gridEl.innerHTML = jobs.map((job) => {
        const jobId = escapeHtml(job.job || "");
        const schedule = escapeHtml(job.schedule || "—");
        const nextRun  = job.next_run ? fmtDate(job.next_run) : "Not scheduled";
        const triggerHtml = isAdmin
          ? `<div class="sched-card__trigger">
               <button class="btn btn--primary" data-job="${jobId}" id="btn-trigger-${jobId}" type="button"
                 style="font-size:12px;padding:7px 14px;">
                 <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                 Run ${jobId}
               </button>
             </div>`
          : "";

        return `
          <div class="sched-card">
            <div class="sched-card__title">${jobId}</div>
            <div class="sched-card__schedule">${schedule}</div>
            <div class="sched-card__next">Next run: <strong>${nextRun}</strong></div>
            ${triggerHtml}
          </div>`;
      }).join("");

      // Wire up trigger buttons
      if (isAdmin) {
        jobs.forEach((job) => {
          const btn = document.getElementById(`btn-trigger-${job.job}`);
          if (btn) btn.addEventListener("click", () => triggerRun(job.job));
        });
      }
    }
  } catch (e) {
    if (e.message !== "HTTP 401") {
      if (gridEl) gridEl.innerHTML = `<p class="empty-state">Could not load scheduler status.</p>`;
    }
  }
}

async function triggerRun(jobType) {
  const feedbackEl = document.getElementById("sched-feedback");
  const btn = document.getElementById(`btn-trigger-${jobType}`);

  // Disable all trigger buttons
  document.querySelectorAll(".sched-card__trigger .btn").forEach((b) => { b.disabled = true; });
  if (feedbackEl) {
    feedbackEl.hidden = false;
    feedbackEl.className = "run-feedback run-feedback--loading";
    feedbackEl.innerHTML = `<span class="spinner"></span> Running ${escapeHtml(jobType)} job — this may take a minute…`;
  }

  try {
    const res = await fetch(`/run/${encodeURIComponent(jobType)}`, {
      method: "POST",
      credentials: "same-origin",
    });
    const data = await res.json().catch(() => ({}));

    if (res.ok && data.status === "success") {
      showSchedFeedback("success", `Run completed. Finished at ${escapeHtml(data.finished_at || "—")}.`);
    } else if (res.ok && data.status === "partial") {
      // PR-ADS-162H: the monthly data refresh reports its own
      // outcome. Partial is neither success nor failure: the run finished and
      // at least one step did not land, so the steps are named.
      // showSchedFeedback() assigns textContent, so the step names need no
      // escaping here.
      const steps = (data.result && Array.isArray(data.result.failed_steps))
        ? data.result.failed_steps.join("; ") : "";
      showSchedFeedback("warning",
        `Run finished partially at ${data.finished_at || "—"}. Did not complete: ${steps || "steps not reported"}.`);
    } else if (res.status === 409) {
      showSchedFeedback("error", `${escapeHtml(jobType)} job is already running.`);
    } else if (res.status === 403) {
      showSchedFeedback("error", "Access denied — admin role required.");
    } else if (res.status === 401) {
      showLogin();
    } else {
      const errMsg = data.error || data.detail || "Unknown error";
      showSchedFeedback("error", `Run failed: ${escapeHtml(errMsg)}`);
    }
  } catch (e) {
    showSchedFeedback("error", `Request failed: ${escapeHtml(e.message)}`);
  } finally {
    document.querySelectorAll(".sched-card__trigger .btn").forEach((b) => { b.disabled = false; });
  }
}

function showSchedFeedback(type, msg) {
  const el = document.getElementById("sched-feedback");
  if (!el) return;
  el.hidden = false;
  el.className = `run-feedback run-feedback--${type}`;
  el.textContent = msg;
}

// ── Historical Backfill page (admin-only) ──────────────────────────────────

async function loadBackfillStatus() {
  // Populate the Latest Backfill Summary panel with the server-side state.
  try {
    const data = await fetchJSON("/api/backfill/status");
    if (data && data.latest) {
      renderBackfillSummary(data.latest);
    }
  } catch (e) {
    if (e.message !== "HTTP 401" && e.message !== "HTTP 403") {
      // Non-fatal — the summary panel stays hidden until a run is triggered.
    }
  }
}

async function runBackfillFromUI(e) {
  e.preventDefault();

  const sourceEl     = document.getElementById("backfill-source");
  const dateFromEl   = document.getElementById("backfill-date-from");
  const dateToEl     = document.getElementById("backfill-date-to");
  const chunkEl      = document.getElementById("backfill-chunk");
  const maxChunksEl  = document.getElementById("backfill-max-chunks");
  const dryRunEl     = document.getElementById("backfill-dry-run");
  const runBtn       = document.getElementById("backfill-run-btn");
  const errorEl      = document.getElementById("backfill-form-error");
  const feedbackEl   = document.getElementById("backfill-feedback");

  // Client-side validation
  const dateFrom = dateFromEl ? dateFromEl.value.trim() : "";
  const dateTo   = dateToEl   ? dateToEl.value.trim()   : "";

  if (!dateFrom) {
    showBackfillError(errorEl, "Date from is required.");
    return;
  }
  if (!dateTo) {
    showBackfillError(errorEl, "Date to is required.");
    return;
  }
  if (new Date(dateFrom) > new Date(dateTo)) {
    showBackfillError(errorEl, "Date from must be on or before date to.");
    return;
  }

  const maxChunksRaw = maxChunksEl ? maxChunksEl.value.trim() : "";
  const maxChunks    = maxChunksRaw ? parseInt(maxChunksRaw, 10) : null;
  if (maxChunks !== null && (isNaN(maxChunks) || maxChunks < 1)) {
    showBackfillError(errorEl, "Max chunks must be a positive integer when provided.");
    return;
  }

  if (errorEl) errorEl.hidden = true;

  const dryRun = dryRunEl ? dryRunEl.checked : true;
  const source = sourceEl ? sourceEl.value : "all";
  const chunk  = chunkEl  ? chunkEl.value  : "monthly";

  // Confirm before non-dry-run run
  if (!dryRun) {
    const confirmed = window.confirm(
      "This will pull historical data from external sources and write it to the local database only.\n\n" +
      "It will NOT modify Google Ads or HubSpot.\n\n" +
      "Continue?"
    );
    if (!confirmed) return;
  }

  // Disable button and show loading state
  if (runBtn) { runBtn.disabled = true; runBtn.textContent = "Running…"; }
  if (feedbackEl) {
    feedbackEl.hidden = false;
    feedbackEl.className = "run-feedback run-feedback--loading";
    feedbackEl.innerHTML = `<span class="spinner"></span> ${dryRun ? "Running dry run" : "Running backfill — local writes only"} — this may take a moment…`;
  }

  try {
    const body = { source, date_from: dateFrom, date_to: dateTo, chunk, dry_run: dryRun };
    if (maxChunks !== null) body.max_chunks = maxChunks;

    const res  = await fetch("/api/backfill/run", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));

    if (res.ok && data.status === "success") {
      if (feedbackEl) {
        feedbackEl.className = "run-feedback run-feedback--success";
        feedbackEl.textContent = `Backfill ${dryRun ? "dry run" : "run"} completed. Finished at ${escapeHtml(data.finished_at || "—")}.`;
      }
      renderBackfillSummary(data);
      // Refresh dataset freshness after a successful non-dry-run
      if (!dryRun) loadDataFreshness();
    } else if (res.status === 409) {
      if (feedbackEl) {
        feedbackEl.className = "run-feedback run-feedback--error";
        feedbackEl.textContent = "A backfill run is already in progress. Please wait.";
      }
    } else if (res.status === 401) {
      showLogin();
    } else if (res.status === 403) {
      if (feedbackEl) {
        feedbackEl.className = "run-feedback run-feedback--error";
        feedbackEl.textContent = "Access denied — admin role required.";
      }
    } else {
      const errMsg = data.detail || data.error || "Unknown error";
      if (feedbackEl) {
        feedbackEl.className = "run-feedback run-feedback--error";
        feedbackEl.textContent = `Backfill failed: ${escapeHtml(errMsg)}`;
      }
    }
  } catch (err) {
    if (feedbackEl) {
      feedbackEl.className = "run-feedback run-feedback--error";
      feedbackEl.textContent = `Request failed: ${escapeHtml(err.message)}`;
    }
  } finally {
    if (runBtn) {
      runBtn.disabled = false;
      runBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg> Run Backfill`;
    }
  }
}

function showBackfillError(el, msg) {
  if (!el) return;
  el.textContent = msg;
  el.hidden = false;
}

function renderBackfillSummary(run) {
  const panelEl  = document.getElementById("backfill-summary-panel");
  const bodyEl   = document.getElementById("backfill-summary-body");
  if (!panelEl || !bodyEl) return;

  panelEl.hidden = false;

  if (!run) {
    bodyEl.innerHTML = buildEmptyState({ pageKey: "backfill", rowsInWindow: 0 });
    return;
  }

  const summary = run.summary || {};
  const datasets = summary.datasets || {};
  const mode = run.dry_run ? "Dry run — no database writes" : "Local persistence — writes to local DB only";
  const status = run.status === "success" ? "success" : (run.status || "unknown");

  const dsRows = Object.entries(datasets).map(([key, ds]) => {
    const st  = escapeHtml(ds.status || "unknown");
    const cls = ds.status === "success" ? "badge--ok"
              : ds.status === "dry_run" || ds.status === "unsupported_by_current_connector" ? "badge--neutral"
              : "badge--error";
    const pulled    = ds.rows_pulled  != null ? ds.rows_pulled  : "—";
    const written   = ds.rows_written != null ? ds.rows_written : "—";
    const chunksPl  = ds.chunks_planned != null ? ds.chunks_planned : null;
    const plannedTxt = chunksPl != null
      ? `${chunksPl} chunk${chunksPl !== 1 ? "s" : ""} planned`
      : "";
    const note = ds.note ? `<div class="backfill-summary__ds-note">${escapeHtml(ds.note)}</div>` : "";

    let metaStats;
    if (run.dry_run) {
      metaStats = plannedTxt
        ? `<span class="backfill-summary__ds-stat">${escapeHtml(plannedTxt)}</span>`
        : "";
    } else {
      metaStats = `<span class="backfill-summary__ds-stat">pulled: ${escapeHtml(String(pulled))}</span>`
        + `<span class="backfill-summary__ds-stat">written: ${escapeHtml(String(written))}</span>`;
    }

    return `
      <div class="backfill-summary__ds-row">
        <div class="backfill-summary__ds-key">${escapeHtml(key)}</div>
        <div class="backfill-summary__ds-meta">
          <span class="badge ${cls}"><span class="dot"></span>${st}</span>
          ${metaStats}
        </div>
        ${note}
      </div>`;
  }).join("");

  bodyEl.innerHTML = `
    <dl class="kv-list" style="margin-bottom:var(--space-4)">
      <dt>Status</dt>      <dd>${statusBadge(status)}</dd>
      <dt>Source</dt>      <dd>${escapeHtml(run.source || "—")}</dd>
      <dt>Date range</dt>  <dd>${escapeHtml(run.date_from || "—")} → ${escapeHtml(run.date_to || "—")}</dd>
      <dt>Chunk</dt>       <dd>${escapeHtml(run.chunk || "—")}</dd>
      <dt>Mode</dt>        <dd>${escapeHtml(mode)}</dd>
      <dt>Chunks</dt>      <dd>${escapeHtml(String(summary.chunks_completed ?? "—"))} / ${escapeHtml(String(summary.chunks_total ?? "—"))} completed</dd>
      <dt>Finished</dt>    <dd>${run.finished_at ? fmtDate(run.finished_at) : "—"}</dd>
    </dl>
    ${dsRows ? `<div class="backfill-summary__datasets">${dsRows}</div>` : ""}
    ${(() => {
      // Collect all errors: summary.errors first, then top-level run.error if not already present.
      const errs = Array.isArray(summary.errors) ? [...summary.errors] : [];
      if (run.error && !errs.includes(run.error)) errs.push(run.error);
      return errs.length > 0
        ? `<div class="run-feedback run-feedback--error" style="margin-top:var(--space-4)">
             <strong>Errors (${errs.length}):</strong><br>
             ${errs.map((e) => escapeHtml(String(e))).join("<br>")}
           </div>`
        : "";
    })()}`;
}

// ── System Health page ─────────────────────────────────────────────────────

// ── CRM Truth Exceptions workbench (PR-ADS-191, admin-only, read-only) ──────
// Lists the HubSpot won deals that stop customer / revenue totals publishing,
// from the canonical read model. Unavailable evidence is shown as unavailable —
// never as "0 issues". Nothing here classifies: every reason comes from the API.

const CTE_STATUS = {
  certified_no_findings:   { cls: "badge--ok",      text: "Certified · no findings" },
  certified_with_findings: { cls: "badge--warning", text: "Certified · CRM findings" },
  source_stale:            { cls: "badge--warning", text: "Source stale" },
  scan_not_current:        { cls: "badge--warning", text: "Scan not current" },
  evidence_unavailable:    { cls: "badge--error",   text: "Evidence unavailable" },
};
const CTE_FAMILY_LABELS = {
  customer_identity: "Customer identity",
  revenue_proof: "Revenue proof",
  won_deal_membership: "Won-deal membership",
};
let _cteSeq = 0;
let _cteOptionsLoaded = false;

// PR-ADS-161E: filters a "View blockers" link asked for, read from the hash
// once and applied as soon as the filter options exist.
function _ctePendingFilters() {
  const q = new URLSearchParams((window.location.hash || "").split("?")[1] || "");
  return { window: q.get("cte_window") || "", family: q.get("cte_family") || "",
           reason: q.get("cte_reason") || "" };
}

function _cteParams() {
  const p = new URLSearchParams();
  // Before the selects are populated, a deep link's filters stand in for them.
  const pending = _cteOptionsLoaded ? { window: "", family: "", reason: "" } : _ctePendingFilters();
  const win = (document.getElementById("cte-window") || {}).value || pending.window;
  if (win) {
    const [type, key] = win.split("/");
    p.set("window_type", type);
    p.set("window_key", key);
  }
  const fam = (document.getElementById("cte-family") || {}).value || pending.family;
  if (fam) p.set("family", fam);
  const rsn = (document.getElementById("cte-reason") || {}).value || pending.reason;
  if (rsn) p.set("reason", rsn);
  const q = ((document.getElementById("cte-search") || {}).value || "").trim();
  if (q) p.set("q", q);
  return p;
}

function _cteCount(value) {
  // NULL is "we could not look" — never rendered as zero.
  return value === null || value === undefined ? "Unavailable" : String(value);
}

function _cteFillOptions(data) {
  // An unavailable response carries no windows or vocabulary: stay retryable
  // so Refresh can populate the filters once the evidence recovers.
  if (_cteOptionsLoaded || !Array.isArray(data.windows) || !data.reason_vocabulary) return;
  const winSel = document.getElementById("cte-window");
  const famSel = document.getElementById("cte-family");
  const rsnSel = document.getElementById("cte-reason");
  (data.windows || []).forEach((w) => {
    winSel.insertAdjacentHTML("beforeend",
      `<option value="${escapeHtml(w.window)}">${escapeHtml(w.label || w.window)} (${escapeHtml(w.window_type)})</option>`);
  });
  Object.entries(data.reason_vocabulary || {}).forEach(([family, reasons]) => {
    famSel.insertAdjacentHTML("beforeend",
      `<option value="${escapeHtml(family)}">${escapeHtml(CTE_FAMILY_LABELS[family] || family)}</option>`);
    reasons.forEach((r) => rsnSel.insertAdjacentHTML("beforeend",
      `<option value="${escapeHtml(r)}">${escapeHtml(r)}</option>`));
  });
  // Select what a "View blockers" link asked for (PR-ADS-161E).
  const pending = _ctePendingFilters();
  if (pending.window) winSel.value = pending.window;
  if (pending.family) famSel.value = pending.family;
  if (pending.reason) rsnSel.value = pending.reason;
  _cteOptionsLoaded = true;
}

function renderCrmTruthSummary(data) {
  const st = CTE_STATUS[data.status] || { cls: "badge--error", text: data.status || "Unknown" };
  const scan = data.association_scan || {};
  const verdict = scan.scan_current || {};
  const fam = (data.totals && data.totals.by_family) || {};
  const famDeals = (f) => (data.totals ? _cteCount((fam[f] || {}).deals) : "Unavailable");
  const blockers = (data.dataset_blockers || []).map((b) =>
    `<li><strong>${escapeHtml(b.code)}</strong> — ${escapeHtml(b.explanation || "")}${b.detail ? ` (${escapeHtml(String(b.detail))})` : ""}</li>`).join("");
  const affected = (data.windows || []).filter((w) =>
    ["customers", "revenue_usd"].some((m) => (w.publication[m] || {}).status !== "published"));
  return `
    <div style="display:flex;align-items:center;gap:var(--space-3);margin-bottom:var(--space-3)">
      <span class="badge ${st.cls}"><span class="dot"></span>${escapeHtml(st.text)}</span>
      <span style="font-size:12px;color:var(--text-muted)">Generated ${escapeHtml(data.generated_at || "")}</span>
    </div>
    <div class="freshness-summary-grid">
      <div><span>Deals flagged won in HubSpot (any close date; not the closed-won count)</span><strong>${_cteCount(data.won_deal_population)}</strong></div>
      <div><span>Association scan</span><strong>${verdict.current === true ? "Current" : escapeHtml(verdict.reason || "Not current")}</strong></div>
      <div><span>Last certified scan</span><strong>${escapeHtml(scan.last_complete_scan_started_at || "None recorded")}</strong></div>
      <div><span>Customer-identity blockers</span><strong>${famDeals("customer_identity")} deal(s)</strong></div>
      <div><span>Revenue-proof blockers</span><strong>${famDeals("revenue_proof")} deal(s)</strong></div>
      <div><span>Won-deal membership blockers</span><strong>${famDeals("won_deal_membership")} deal(s)</strong></div>
      <div><span>Windows with withheld customers / revenue</span><strong>${data.windows ? affected.length : "Unavailable"}</strong></div>
    </div>
    ${blockers ? `<ul class="freshness-help-note" style="margin-top:var(--space-3)">${blockers}</ul>` : ""}`;
}

function renderCrmTruthWindows(data) {
  if (!data.windows) return "";
  const pubCell = (p) => p.status === "published"
    ? '<span class="status-pill status-pill--ok">Published</span>'
    : `<span class="status-pill status-pill--missing">${escapeHtml(p.status || "unknown")}</span> <small>${escapeHtml(p.reason || "")}</small>`;
  const explainedCell = (w) => {
    const bad = ["customers", "revenue_usd"].filter((m) => ((w.explanation || {})[m] || {}).explained !== true);
    return bad.length
      ? `<span class="status-pill status-pill--missing">Not explained</span> <small>${escapeHtml(bad.join(", "))}</small>`
      : '<span class="status-pill status-pill--ok">Explained</span>';
  };
  const rows = data.windows.map((w) => `
    <tr>
      <td>${escapeHtml(w.label || w.window)} <small>${escapeHtml(w.window_type)}</small></td>
      <td>${pubCell(w.publication.customers)}</td>
      <td>${pubCell(w.publication.revenue_usd)}</td>
      <td>${_cteCount(w.blocker_deals.customer_identity_any)}</td>
      <td>${_cteCount(w.blocker_deals.revenue_unproven)}</td>
      <td>${_cteCount(w.blocker_deals.won_deal_membership)}</td>
      <td>${explainedCell(w)}</td>
    </tr>`).join("");
  return `
    <div class="dataset-freshness-table-wrap" style="margin-bottom:var(--space-4)">
      <table class="data-table dataset-freshness-table">
        <thead><tr><th>Window</th><th>Customers</th><th>Revenue (USD)</th><th>Identity blockers</th><th>Revenue blockers</th><th>Membership blockers</th><th>Verdict explained</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function renderCrmTruthRows(data) {
  if (data.rows === null || data.rows === undefined) {
    if (data.available && Array.isArray(data.complete_families)) {
      const readable = data.complete_families.map((f) => CTE_FAMILY_LABELS[f] || f).join(" or ");
      return `<p class="empty-state">Customer-identity evidence could not be read, so this list would be incomplete. Filter by ${escapeHtml(readable)} to see those rows. This is not "0 issues".</p>`;
    }
    return '<p class="empty-state">Evidence unavailable — the exception list cannot be produced. This is not "0 issues".</p>';
  }
  const pg = data.pagination || {};
  if (!data.rows.length) {
    return data.status === "certified_no_findings" && !_cteParams().toString()
      ? '<p class="empty-state">Certified: no CRM truth exceptions.</p>'
      : '<p class="empty-state">No exceptions match these filters.</p>';
  }
  const body = data.rows.map((r) => {
    const link = r.hubspot_record_url
      ? `<a href="${escapeHtml(r.hubspot_record_url)}" target="_blank" rel="noopener noreferrer">Open in HubSpot</a>`
      : '<span class="muted">Portal not configured</span>';
    const companies = r.associated_company_ids === null
      ? "Unavailable"
      : (r.associated_company_ids.length ? r.associated_company_ids.map(escapeHtml).join(", ") : "None");
    const amount = r.amount_raw === null || r.amount_raw === undefined
      ? "Missing" : `${escapeHtml(String(r.amount_raw))} ${escapeHtml(r.deal_currency_code || "(no currency)")}`;
    return `
      <tr>
        <td><strong>${escapeHtml(r.deal_name || "(unnamed)")}</strong><br><small>${escapeHtml(r.deal_id)}</small></td>
        <td>${escapeHtml(CTE_FAMILY_LABELS[r.exception_family] || r.exception_family)}</td>
        <td><code>${escapeHtml(r.reason_code)}</code><br><small>${escapeHtml(r.explanation || "")}</small></td>
        <td>${escapeHtml(r.deal_close_date || "Missing")}</td>
        <td>${amount}</td>
        <td>${r.revenue_usd === null || r.revenue_usd === undefined ? "Not proven" : escapeHtml(String(r.revenue_usd))}</td>
        <td>${companies}</td>
        <td>${r.affected_windows.length ? r.affected_windows.length
          : (r.close_date_state === "invalid_close_date" ? "None yet <small>(close date after today)</small>" : "None published <small>(deal count withheld)</small>")}</td>
        <td>${link}</td>
      </tr>`;
  }).join("");
  return `
    <p style="font-size:12px;color:var(--text-muted)">Showing ${pg.returned} of ${pg.total} exception row(s).</p>
    <div class="dataset-freshness-table-wrap">
      <table class="data-table dataset-freshness-table">
        <thead><tr><th>Deal</th><th>Family</th><th>Reason</th><th>Close date</th><th>Amount</th><th>Proven USD</th><th>Companies</th><th>Windows</th><th>HubSpot</th></tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>`;
}

async function loadCrmTruthExceptions() {
  const summary = document.getElementById("cte-summary");
  if (!summary) return;
  const token = ++_cteSeq;
  const params = _cteParams();
  const exportBtn = document.getElementById("cte-export-btn");
  if (exportBtn) exportBtn.href = `/api/audit/crm-truth-exceptions.csv${params.toString() ? "?" + params : ""}`;
  params.set("limit", "1000");
  try {
    const data = await fetchJSON(`/api/audit/crm-truth-exceptions?${params}`);
    if (token !== _cteSeq) return;  // superseded by a newer request
    _cteFillOptions(data);
    document.getElementById("cte-controls").hidden = false;
    summary.innerHTML = renderCrmTruthSummary(data);
    document.getElementById("cte-windows").innerHTML = renderCrmTruthWindows(data);
    document.getElementById("cte-table").innerHTML = renderCrmTruthRows(data);
  } catch (e) {
    if (token !== _cteSeq) return;
    if (e.message === "HTTP 401") return;
    // An API failure is its own state: never "0 issues".
    summary.innerHTML = `<p class="empty-state"><span class="badge badge--error"><span class="dot"></span>Could not load</span> CRM truth exceptions are unavailable: ${escapeHtml(e.message)}</p>`;
    document.getElementById("cte-windows").innerHTML = "";
    document.getElementById("cte-table").innerHTML = "";
  }
}

async function loadHealth() {
  renderRunMeta("health");
  const el = document.getElementById("health-body");
  if (!el) return;

  el.innerHTML = `<p class="empty-state">Loading readiness data…</p>`;

  try {
    const data = await fetchJSON("/readiness");
    const checks = data.checks || {};

    const overallCls = data.status === "pass" ? "badge--ok" : "badge--error";
    const overallBadge = `<span class="badge ${overallCls}"><span class="dot"></span>${escapeHtml(data.status || "unknown")}</span>`;

    const renderGroup = (title, obj) => {
      if (!obj || typeof obj !== "object") return "";
      const rows = Object.entries(obj).map(([key, value]) => {
        const pillCls  = value === true ? "status-pill--ok" : value === false ? "status-pill--missing" : "status-pill--optional";
        const pillText = value === true ? "OK" : value === false ? "Missing" : "Optional";
        return `
          <div class="health-row">
            <span class="health-row__key">${escapeHtml(key)}</span>
            <span class="status-pill ${pillCls}">${pillText}</span>
          </div>`;
      }).join("");
      return `
        <div class="panel" style="margin-bottom:0">
          <div class="panel__header">${escapeHtml(title)}</div>
          <div class="panel__body panel__body--flush">${rows}</div>
        </div>`;
    };

    el.innerHTML = `
      <div style="display:flex;align-items:center;gap:var(--space-3);margin-bottom:var(--space-5)">
        <span style="font-size:13px;font-weight:500;color:var(--text-secondary)">Overall status:</span>
        ${overallBadge}
      </div>
      <div class="health-grid">
        <div style="display:flex;flex-direction:column;gap:var(--space-4)">
          ${renderGroup("Config Files", checks.config_files)}
          ${renderGroup("Directories", checks.directories)}
        </div>
        <div style="display:flex;flex-direction:column;gap:var(--space-4)">
          ${renderGroup("Documentation", checks.docs)}
          ${renderGroup("Module Imports", checks.imports)}
        </div>
      </div>`;
  } catch (e) {
    if (e.message === "HTTP 401") return;
    if (e.message === "HTTP 403") {
      navigate("dashboard", { updateHash: true, replace: true });
      return;
    }
    el.innerHTML = `<p class="empty-state">Could not load readiness data. Admin access required.</p>`;
  }

  // Load dataset freshness panel alongside readiness checks
  loadDatasetFreshness();
  loadGclidCoverage();
  loadSearchTermsVerdict();
  loadWarRoom();
  loadCrmTruthExceptions();
  wireKeywordRefreshButton();
}

// PR-ADS-146A §5 — admin-only "Refresh Keyword Data" action. Pulls the recent
// rolling incremental into PostgreSQL (local writes only); never mutates Google
// Ads. Concurrency is guarded server-side (409 while a run is in flight).
function wireKeywordRefreshButton() {
  const btn = document.getElementById("kw-refresh-btn");
  if (!btn) return;
  const isAdmin = _currentUser && _currentUser.role === "admin";
  btn.hidden = !isAdmin;
  if (!isAdmin || btn.dataset.wired === "1") return;
  btn.dataset.wired = "1";
  btn.addEventListener("click", runKeywordRefresh);
}

async function runKeywordRefresh() {
  const btn = document.getElementById("kw-refresh-btn");
  const fb = document.getElementById("kw-refresh-feedback");
  if (btn) btn.disabled = true;
  if (fb) {
    fb.hidden = false;
    fb.className = "run-feedback run-feedback--loading";
    fb.innerHTML = `<span class="spinner"></span> Refreshing recent keyword facts — local write only, Google Ads is read-only…`;
  }
  try {
    const res = await fetch("/api/keyword-evidence/refresh", {
      method: "POST",
      credentials: "same-origin",
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.status === "success") {
      const r = data.result || {};
      const wrote = r.written !== undefined && r.written !== null ? `${r.written} facts written` : "completed";
      const range = r.date_from && r.date_to ? ` (${escapeHtml(r.date_from)} → ${escapeHtml(r.date_to)})` : "";
      showKwRefreshFeedback("success", `Keyword data refreshed — ${escapeHtml(String(wrote))}${range}.`);
      loadDatasetFreshness();
    } else if (res.status === 409) {
      showKwRefreshFeedback("error", "A keyword refresh is already running — please wait for it to finish.");
    } else if (res.status === 403) {
      showKwRefreshFeedback("error", "Access denied — admin role required.");
    } else if (res.status === 401) {
      showLogin();
    } else {
      const msg = data.error || data.detail || (data.result && data.result.error) || "Unknown error";
      showKwRefreshFeedback("error", `Refresh failed: ${escapeHtml(String(msg))}`);
    }
  } catch (e) {
    showKwRefreshFeedback("error", `Request failed: ${escapeHtml(e.message)}`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function showKwRefreshFeedback(type, msg) {
  const el = document.getElementById("kw-refresh-feedback");
  if (!el) return;
  el.hidden = false;
  el.className = `run-feedback run-feedback--${type}`;
  el.innerHTML = msg;
}

// ── System Status War Room (PR-ADS-068) ────────────────────────────────────

async function loadWarRoom() {
  const el = document.getElementById("war-room-body");
  if (!el) return;

  el.innerHTML = `<p class="empty-state"><span class="spinner"></span> Loading system status…</p>`;

  try {
    const data = await fetchJSON("/api/system/status-war-room?days=60");
    renderWarRoom(el, data);
  } catch (e) {
    if (e.message === "HTTP 401" || e.message === "HTTP 403") {
      el.innerHTML = `<p class="empty-state">Admin access required for War Room.</p>`;
      return;
    }
    el.innerHTML = `<p class="empty-state">Could not load system status.</p>`;
  }
}

function renderWarRoom(el, data) {
  const statusCls = data.overall_status === "ok" ? "war-room-status--ok"
    : data.overall_status === "error" ? "war-room-status--error"
    : data.overall_status === "warning" ? "war-room-status--warning"
    : "war-room-status--neutral";

  const blockedCount = (data.page_impact || []).length;
  const warningCount = (data.summary || {}).warning || 0;
  const errorCount = (data.summary || {}).error || 0;

  // Hero
  let html = `
    <div class="war-room-hero ${statusCls}">
      <div class="war-room-hero__status">
        <span class="war-room-hero__label">System Status:</span>
        <span class="war-room-hero__value">${escapeHtml((data.overall_status || "unknown").toUpperCase())}</span>
      </div>
      <p class="war-room-hero__desc">${escapeHtml(data.overall_label || "")}</p>
      <p class="war-room-hero__meta">${blockedCount} blocked page${blockedCount !== 1 ? "s" : ""} · ${warningCount} warning${warningCount !== 1 ? "s" : ""} · ${errorCount} critical failure${errorCount !== 1 ? "s" : ""}</p>
      <p class="war-room-hero__ts">Generated: ${data.generated_at ? new Date(data.generated_at).toLocaleString() : "—"} · Window: ${data.days || 60} days</p>
    </div>`;

  // Critical Blockers
  const blockers = data.critical_blockers || [];
  if (blockers.length > 0) {
    html += `<div class="war-room-section"><h3 class="war-room-section__title">Critical Blockers</h3><div class="war-room-blockers">`;
    for (const b of blockers) {
      const bCls = b.severity === "error" ? "war-room-blocker--error" : "war-room-blocker--warning";
      html += `
        <div class="war-room-blocker ${bCls}">
          <div class="war-room-blocker__title">${escapeHtml(b.title)}</div>
          <div class="war-room-blocker__affects">Affects: ${escapeHtml((b.affected_pages || []).join(", "))}</div>
          <div class="war-room-blocker__action">Next: ${escapeHtml(b.next_action || "")}</div>
        </div>`;
    }
    html += `</div></div>`;
  }

  // Pipeline Dependency Map
  const pipelines = data.pipelines || [];
  if (pipelines.length > 0) {
    html += `<div class="war-room-section"><h3 class="war-room-section__title">Pipeline Dependency Map</h3>`;
    html += `<p class="war-room-section__note">Search Terms is the raw evidence layer. Waste Terms and N-Grams depend on it.</p>`;
    html += `<div class="war-room-table-wrap"><table class="war-room-table"><thead><tr>
      <th>Pipeline</th><th>Status</th><th>Rows</th><th>Blocks</th><th>Next Action</th>
    </tr></thead><tbody>`;
    for (const p of pipelines) {
      const sevCls = p.severity === "ok" ? "sev--ok" : p.severity === "error" ? "sev--error" : p.severity === "warning" ? "sev--warning" : "sev--neutral";
      const blocksStr = (p.blocks || []).map(b => b.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase())).join(", ") || "—";
      const statusLabel = (p.canonical_status || "unknown").replace(/_/g, " ");
      html += `<tr>
        <td>${p.source ? _sourceDisplayName(p.source) : ""} → ${escapeHtml(p.label || "")}</td>
        <td><span class="war-room-pill ${sevCls}">${escapeHtml(statusLabel)}</span></td>
        <td>${p.rows_in_window != null ? Number(p.rows_in_window).toLocaleString() : "—"}</td>
        <td>${escapeHtml(blocksStr)}</td>
        <td>${escapeHtml(p.next_action || "—")}</td>
      </tr>`;
    }
    html += `</tbody></table></div></div>`;
  }

  // Source Health Cards
  const sources = data.sources || [];
  if (sources.length > 0) {
    html += `<div class="war-room-section"><h3 class="war-room-section__title">Source Health</h3><div class="war-room-sources">`;
    for (const s of sources) {
      const sCls = s.status === "ok" ? "war-room-source--ok" : s.status === "error" ? "war-room-source--error" : s.status === "warning" ? "war-room-source--warning" : "war-room-source--neutral";
      html += `
        <div class="war-room-source ${sCls}">
          <div class="war-room-source__label">${escapeHtml(s.label || s.source)}</div>
          <div class="war-room-source__status">${escapeHtml((s.status || "unknown").toUpperCase())}</div>
          <div class="war-room-source__datasets">${(s.datasets || []).map(d => escapeHtml(d.replace(/_/g, " "))).join(", ")}</div>
          <div class="war-room-source__sync">Last sync: ${s.last_successful_sync_at ? new Date(s.last_successful_sync_at).toLocaleString() : "—"}</div>
          <div class="war-room-source__action">${escapeHtml(s.next_action || "")}</div>
        </div>`;
    }
    html += `</div></div>`;
  }

  // Scheduler Timeline
  const sched = data.scheduler || {};
  html += `<div class="war-room-section"><h3 class="war-room-section__title">Scheduler Latest Runs</h3><div class="war-room-scheduler">`;
  // PR-ADS-165A: no Weekly tile — the weekly job is retired, so its last run
  // is history and not a live health signal.
  for (const [key, label] of [["latest_daily", "Daily"], ["latest_monthly", "Monthly"], ["latest_incremental", "Incremental"]]) {
    const run = sched[key];
    const runStatus = run ? run.status : "not_run";
    // PR-ADS-163B: a partial run is not the same grey as "never ran".
    const rCls = runStatus === "success" ? "war-room-run--ok" : runStatus === "failed" ? "war-room-run--error" : runStatus === "partial" ? "war-room-run--warn" : "war-room-run--neutral";
    html += `
      <div class="war-room-run ${rCls}">
        <div class="war-room-run__label">${escapeHtml(label)}</div>
        <div class="war-room-run__status">${escapeHtml(runStatus)}</div>
        <div class="war-room-run__time">${run && run.started_at ? new Date(run.started_at).toLocaleString() : "—"}</div>
      </div>`;
  }
  html += `</div></div>`;

  // Page Impact (PR-ADS-095: status is now ok/degraded/action_needed/blocked)
  const impacts = data.page_impact || [];
  if (impacts.length > 0) {
    const _impactStatusLabel = {
      blocked:        "Blocked",
      degraded:       "Degraded",
      action_needed:  "Action Needed",
      ok:             "OK",
      unknown:        "Unknown",
    };
    html += `<div class="war-room-section"><h3 class="war-room-section__title">Page Impact</h3><div class="war-room-impacts">`;
    for (const imp of impacts) {
      const statusLabel = _impactStatusLabel[imp.status] || escapeHtml(imp.status || "Unknown");
      html += `
        <div class="war-room-impact">
          <span class="war-room-impact__page">${escapeHtml(imp.page)}</span>
          <span class="war-room-impact__status">${escapeHtml(statusLabel)}</span>
          <span class="war-room-impact__reason">${escapeHtml(imp.reason || "")}</span>
        </div>`;
    }
    html += `</div></div>`;
  }

  el.innerHTML = html;
}

// War Room refresh button
document.addEventListener("DOMContentLoaded", () => {
  const btn = document.getElementById("war-room-refresh-btn");
  if (btn) btn.addEventListener("click", () => loadWarRoom());
});

// ── Dataset Freshness ──────────────────────────────────────────────────────

function datasetRelatedPage(source, dataset) {
  const key = `${source}/${dataset}`;
  const map = {
    "google_ads_api/search_terms": { page: "search-terms", label: "Search Term Universe" },
    "google_ads_api/keyword_facts": { page: "search-terms", label: "Keywords & Search Terms" },
    "google_ads_api/canonical_geo": { page: "roas-countries", label: "Countries" },
    "google_ads_api/canonical_spend": { page: "campaigns",  label: "Campaigns"    },
    "hubspot/contacts":     { page: "pipeline",      label: "Pipeline" },
    "hubspot/deals":        { page: "pipeline",      label: "Pipeline"     },
    "gclid/matches":        { page: "gclid-attribution", label: "GCLID Attribution" },
  };
  return map[key] || null;
}

// Derive display status — adds "stale" as a UI-only concept for old successes.
// Threshold is display-only; it does not change backend sync_state.
// Uses config-driven _staleAfterDays (updated from /api/config/ui-thresholds)
// so dataset-level strips and the System Health table share the same threshold.

function datasetDisplayStatus(row) {
  if (row.status !== "success") return row.status || "unknown";
  if (!row.last_successful_sync_at) return "success";
  const ageDays = (Date.now() - new Date(row.last_successful_sync_at).getTime()) / 86400000;
  if (ageDays > _staleAfterDays) return "stale";
  return "success";
}

function renderDatasetFreshnessLoading() {
  const tableEl = document.getElementById("dataset-freshness-table");
  const summaryEl = document.getElementById("dataset-freshness-summary");
  if (summaryEl) summaryEl.innerHTML = "";
  if (tableEl) tableEl.innerHTML = `<p class="empty-state"><span class="spinner"></span> Loading dataset freshness…</p>`;
}

function renderDatasetFreshness(data) {
  const summaryEl = document.getElementById("dataset-freshness-summary");
  const tableEl   = document.getElementById("dataset-freshness-table");
  const helpEl    = document.getElementById("dataset-freshness-help");

  if (!tableEl) return;

  // DB unavailable
  if (datasetFreshnessStatus === "db_unavailable") {
    if (summaryEl) summaryEl.innerHTML = "";
    tableEl.innerHTML = `<p class="empty-state freshness-help-note">Dataset freshness temporarily unavailable — database offline.</p>`;
    if (helpEl) helpEl.hidden = true;
    return;
  }

  // Error
  if (datasetFreshnessStatus === "error") {
    if (summaryEl) summaryEl.innerHTML = "";
    tableEl.innerHTML = `<p class="empty-state">Could not load dataset freshness. Check API health or run status.</p>`;
    if (helpEl) helpEl.hidden = true;
    return;
  }

  // Empty
  if (datasetFreshnessStatus === "empty") {
    if (summaryEl) summaryEl.innerHTML = "";
    tableEl.innerHTML = `<p class="empty-state">No dataset freshness records found yet.</p>`;
    if (helpEl) helpEl.hidden = true;
    return;
  }

  // Summary cards
  let totalCount = 0;
  let freshCount = 0;
  let staleCount = 0;
  let failedCount = 0;
  let runningCount = 0;
  let unknownCount = 0;
  const canonicalSummary = (data && data.canonical_summary) ? data.canonical_summary : null;
  if (canonicalSummary) {
    const getCanonicalCount = (key) => Number(canonicalSummary[key] || 0);
    freshCount = getCanonicalCount("fresh_with_data");
    staleCount = getCanonicalCount("stale_with_data")
      + getCanonicalCount("fresh_but_empty")
      + getCanonicalCount("dependency_blocked");
    failedCount = getCanonicalCount("failed")
      + getCanonicalCount("stale_and_empty")
      + getCanonicalCount("db_unavailable");
    runningCount = getCanonicalCount("running");
    unknownCount = getCanonicalCount("unknown")
      + getCanonicalCount("not_run");
    totalCount = freshCount + staleCount + failedCount + runningCount + unknownCount;
  } else {
    const summary = (data && data.summary) ? data.summary : (() => {
      const s = { total: 0, success: 0, failed: 0, running: 0, unknown: 0, stale: 0 };
      for (const row of datasetFreshnessRows) {
        s.total++;
        const ds = datasetDisplayStatus(row);
        if (ds === "success")      s.success++;
        else if (ds === "failed")  s.failed++;
        else if (ds === "running") s.running++;
        else if (ds === "stale")   s.stale++;
        else                       s.unknown++;
      }
      return s;
    })();
    staleCount = 0;
    for (const row of datasetFreshnessRows) {
      if (datasetDisplayStatus(row) === "stale") staleCount++;
    }
    totalCount = summary.total || 0;
    freshCount = (summary.success || 0) - staleCount;
    failedCount = summary.failed || 0;
    runningCount = summary.running || 0;
    unknownCount = summary.unknown || 0;
  }

  if (summaryEl) {
    summaryEl.innerHTML = `
      <div class="freshness-summary-card">
      <div class="freshness-summary-card__value">${totalCount}</div>
        <div class="freshness-summary-card__label">Total Datasets</div>
      </div>
      <div class="freshness-summary-card freshness-summary-card--success">
        <div class="freshness-summary-card__value">${freshCount}</div>
        <div class="freshness-summary-card__label">Fresh</div>
      </div>
      ${staleCount > 0 ? `
      <div class="freshness-summary-card freshness-summary-card--stale">
        <div class="freshness-summary-card__value">${staleCount}</div>
        <div class="freshness-summary-card__label">Possibly Stale</div>
      </div>` : ""}
      <div class="freshness-summary-card freshness-summary-card--failed">
        <div class="freshness-summary-card__value">${failedCount}</div>
        <div class="freshness-summary-card__label">Failed</div>
      </div>
      <div class="freshness-summary-card freshness-summary-card--running">
        <div class="freshness-summary-card__value">${runningCount}</div>
        <div class="freshness-summary-card__label">Running</div>
      </div>
      <div class="freshness-summary-card freshness-summary-card--unknown">
        <div class="freshness-summary-card__value">${unknownCount}</div>
        <div class="freshness-summary-card__label">Unknown</div>
      </div>`;
  }

  // Table rows — use canonical_status from backend (PR-ADS-067) when available,
  // fall back to legacy datasetDisplayStatus() for backward compat.
  const _canonicalBadgeCls = {
    fresh_with_data:                      "freshness-status-fresh-with-data",
    fresh_but_empty:                      "freshness-status-fresh-but-empty",
    stale_with_data:                      "freshness-status-stale-with-data",
    stale_and_empty:                      "freshness-status-stale-and-empty",
    failed:                               "freshness-status-failed",
    running:                              "freshness-status-running",
    not_run:                              "freshness-status-not-run",
    dependency_blocked:                   "freshness-status-dependency-blocked",
    db_unavailable:                       "freshness-status-db-unavailable",
    unknown:                              "freshness-status-unknown",
    // PR-ADS-095 refined states
    data_available_latest_sync_failed:    "freshness-status-stale-with-data",
    failed_no_data:                       "freshness-status-failed",
    not_run_but_derivable:                "freshness-status-stale-with-data",
    not_run_no_upstream_data:             "freshness-status-not-run",
    unknown_row_count:                    "freshness-status-unknown",
    row_count_not_enabled:                "freshness-status-unknown",
    blocked_by_dependency:                "freshness-status-dependency-blocked",
    empty_success:                        "freshness-status-fresh-but-empty",
  };
  const _canonicalBadgeLabel = {
    fresh_with_data:                      "Fresh with data",
    fresh_but_empty:                      "Fresh but empty",
    stale_with_data:                      "Stale with data",
    stale_and_empty:                      "Stale and empty",
    failed:                               "Failed",
    running:                              "Running",
    not_run:                              "Not run",
    dependency_blocked:                   "Blocked",
    db_unavailable:                       "DB unavailable",
    unknown:                              "Unknown",
    // PR-ADS-095 refined states
    data_available_latest_sync_failed:    "Data Available, Latest Sync Failed",
    failed_no_data:                       "Failed, No Data",
    not_run_but_derivable:                "Not Run, But Derivable",
    not_run_no_upstream_data:             "Not Run, No Upstream Data",
    unknown_row_count:                    "Row Count Unavailable",
    row_count_not_enabled:                "Row Count Not Enabled",
    blocked_by_dependency:                "Blocked by Dependency",
    empty_success:                        "Empty Success",
  };

  const rows = datasetFreshnessRows.map((row) => {
    // Canonical status from backend (PR-ADS-067)
    const cs = row.canonical_status || null;
    let badgeCls, badgeLabel;
    if (cs && _canonicalBadgeCls[cs]) {
      badgeCls = _canonicalBadgeCls[cs];
      badgeLabel = _canonicalBadgeLabel[cs];
    } else {
      // Legacy fallback
      const ds = datasetDisplayStatus(row);
      badgeCls = {
        success: "freshness-status-success",
        failed:  "freshness-status-failed",
        running: "freshness-status-running",
        unknown: "freshness-status-unknown",
        stale:   "freshness-status-stale",
      }[ds] || "freshness-status-unknown";
      badgeLabel = {
        success: "Fresh",
        failed:  "Failed",
        running: "Running",
        unknown: "Unknown",
        stale:   "Stale",
      }[ds] || escapeHtml(ds);
    }

    const related = datasetRelatedPage(row.source, row.dataset);
    const relatedCell = related
      ? `<button class="freshness-related-link btn btn--ghost btn--sm" type="button" data-page="${escapeHtml(related.page)}">${escapeHtml(related.label)}</button>`
      : `<span class="td--na">—</span>`;

    // Reason / next_action tooltip (PR-ADS-067)
    const reasonTip = row.reason ? ` title="${escapeHtml(row.reason + (row.next_action ? ' Next: ' + row.next_action : ''))}"` : "";
    const rowsCell = row.rows_in_window != null ? String(row.rows_in_window) : '<span class="td--na">—</span>';

    return `
      <tr>
        <td>${row.source ? _sourceDisplayName(row.source) : "—"}</td>
        <td class="td--name">${escapeHtml(fmt(row.dataset).replace(/_/g, " "))}${row.refresh_cadence === "monthly" ? ' <span class="td--na" title="Written by the monthly job only (1st of the month). Fresh means refreshed on that schedule, not recent.">· refreshed monthly</span>' : ""}</td>
        <td><span class="freshness-status-badge ${badgeCls}"${reasonTip}>${badgeLabel}</span></td>
        <td>${rowsCell}</td>
        <td>${row.last_successful_sync_at ? fmtDate(row.last_successful_sync_at) : '<span class="td--na">—</span>'}</td>
        <td>${row.latest_source_date || row.last_source_date ? escapeHtml(row.latest_source_date || row.last_source_date) : '<span class="td--na">—</span>'}</td>
        <td class="td--reason">${row.reason ? escapeHtml(row.reason) : '<span class="td--na">—</span>'}</td>
        <td class="td--action">${row.next_action ? escapeHtml(row.next_action) : '<span class="td--na">—</span>'}</td>
        <td>${relatedCell}</td>
      </tr>`;
  }).join("");

  tableEl.innerHTML = `
    <div class="dataset-freshness-table-scroll">
      <table class="data-table dataset-freshness-table" aria-label="Dataset freshness status">
        <thead>
          <tr>
            <th>Source</th>
            <th>Dataset</th>
            <th>Canonical Status</th>
            <th>Rows</th>
            <th>Last Sync</th>
            <th>Latest Source Date</th>
            <th>Reason</th>
            <th>Next Action</th>
            <th>Page</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;

  // Wire related-page buttons (use event delegation on the table wrapper)
  tableEl.querySelectorAll(".freshness-related-link").forEach((btn) => {
    btn.addEventListener("click", () => navigate(btn.dataset.page));
  });

  // Help notes (PR-ADS-067 canonical states)
  if (helpEl) {
    helpEl.hidden = false;
    helpEl.innerHTML = `
      <strong>Fresh with data</strong> — sync succeeded recently and rows exist.<br>
      <strong>Fresh but empty</strong> — sync succeeded but no rows in the window (warning).<br>
      <strong>Stale with data</strong> — rows exist but last sync is older than ${_staleAfterDays} days.<br>
      <strong>Stale and empty</strong> — no rows and no recent sync (critical).<br>
      <strong>Failed</strong> — latest sync or batch failed.<br>
      <strong>Running</strong> — sync in progress.<br>
      <strong>Not run</strong> — no sync has been recorded yet.<br>
      <strong>Blocked</strong> — depends on another dataset that is unavailable.<br>
      <strong>DB unavailable</strong> — database connection issue.`;
  }
}

async function loadDatasetFreshness() {
  datasetFreshnessStatus = "loading";
  renderDatasetFreshnessLoading();

  try {
    const data = await fetchJSON("/api/datasets/freshness");

    if (data.db_unavailable) {
      datasetFreshnessStatus = "db_unavailable";
      datasetFreshnessRows = [];
      _datasetFreshnessByKey = {};
      renderDatasetFreshness(data);
      return;
    }

    datasetFreshnessRows = data.datasets || [];

    // Populate per-key lookup used by renderPageDatasetFreshness().
    _datasetFreshnessByKey = {};
    for (const row of datasetFreshnessRows) {
      const key = `${row.source}/${row.dataset}`;
      _datasetFreshnessByKey[key] = row;
    }

    if (!datasetFreshnessRows.length) {
      datasetFreshnessStatus = "empty";
    } else {
      datasetFreshnessStatus = "ok";
    }

    renderDatasetFreshness(data);

    // Re-render the current page's freshness strip now that the cache is
    // populated.  This resolves the race where renderPageDatasetFreshness()
    // fell back to renderRunMeta() on fast first navigation.
    if (_currentPage) {
      const sectionKey = _currentPage.replace(/-/g, "_");
      if (PAGE_DATASET_MAP[sectionKey]) {
        renderPageDatasetFreshness(sectionKey);
      }
    }
  } catch (err) {
    if (err && err.message === "HTTP 401") return;
    datasetFreshnessStatus = "error";
    datasetFreshnessRows = [];
    _datasetFreshnessByKey = {};
    renderDatasetFreshness();
  }
}

// ── Search Terms Pipeline Verdict (PR-ADS-066) ───────────────────────────

async function loadSearchTermsVerdict() {
  const bodyEl = document.getElementById("search-terms-verdict-body");
  const refreshBtn = document.getElementById("search-terms-verdict-refresh-btn");
  if (!bodyEl) return;

  // Wire refresh button
  if (refreshBtn && !refreshBtn._wired) {
    refreshBtn._wired = true;
    refreshBtn.addEventListener("click", () => loadSearchTermsVerdict());
  }

  bodyEl.innerHTML = `<p class="empty-state"><span class="spinner"></span> Loading Search Terms verdict…</p>`;

  try {
    const data = await fetchJSON("/api/system/search-terms-verdict?days=60");

    const verdictColorCls = _verdictColorClass(data.verdict);
    const cards = [
      { label: "Verdict", value: data.verdict || "—", cls: verdictColorCls },
      { label: "Rows (60d)", value: data.db ? fmt(data.db.rows_60d) : "—" },
      { label: "Latest Source Date", value: data.db && data.db.latest_source_date ? escapeHtml(data.db.latest_source_date) : "—" },
      { label: "Sync Status", value: data.sync && data.sync.sync_state_status ? escapeHtml(data.sync.sync_state_status) : "—" },
      { label: "Latest Batch Rows", value: data.sync && data.sync.latest_batch_row_count != null ? fmt(data.sync.latest_batch_row_count) : "—" },
      { label: "Next Action", value: data.next_action ? escapeHtml(data.next_action) : "—", wide: true },
    ];

    bodyEl.innerHTML = `
      <div class="verdict-cards-grid">
        ${cards.map(c => `
          <div class="verdict-card${c.wide ? " verdict-card--wide" : ""}${c.cls ? " " + c.cls : ""}">
            <div class="verdict-card__value">${c.value}</div>
            <div class="verdict-card__label">${escapeHtml(c.label)}</div>
          </div>
        `).join("")}
      </div>
      <p style="font-size:12px;color:var(--text-muted);margin-top:var(--space-3)">
        Generated: ${data.generated_at ? escapeHtml(data.generated_at) : "—"}
        ${data.reason ? " — " + escapeHtml(data.reason) : ""}
      </p>`;
  } catch (e) {
    if (e.message === "HTTP 401" || e.message === "HTTP 403") {
      bodyEl.innerHTML = `<p class="empty-state">Admin access required.</p>`;
      return;
    }
    bodyEl.innerHTML = `<p class="empty-state">Could not load Search Terms verdict. Check API health.</p>`;
  }
}

function _verdictColorClass(verdict) {
  if (!verdict) return "";
  switch (verdict) {
    case "OK": return "verdict-card--ok";
    case "FRESH_BUT_EMPTY":
    case "WINDSOR_PULL_EMPTY": return "verdict-card--warn";
    case "DB_WRITE_FAILED":
    case "DB_UNAVAILABLE":
    case "DB_HAS_ROWS_API_EMPTY": return "verdict-card--error";
    case "NOT_DEPLOYED_OR_NOT_RUN_AFTER_DEPLOYMENT": return "verdict-card--gray";
    default: return "verdict-card--warn";
  }
}

// ── GCLID Coverage (optional panel) ───────────────────────────────────────

async function loadGclidCoverage() {
  const panel  = document.getElementById("gclid-coverage-panel");
  const bodyEl = document.getElementById("gclid-coverage-body");
  if (!panel || !bodyEl) return;

  try {
    const days = getSelectedDays();
    const data = await fetchJSON(`/api/gclid-coverage?days=${days}`);

    if (data.db_unavailable) {
      // Silently hide if unavailable
      panel.hidden = true;
      return;
    }

    const rows = data.rows || [];
    if (!rows.length) {
      panel.hidden = true;
      return;
    }

    // Show only the most recent snapshot
    const latest = rows[rows.length - 1];
    panel.hidden = false;
    bodyEl.innerHTML = `
      <div class="freshness-summary-grid" style="margin-bottom:0">
        <div class="freshness-summary-card">
          <div class="freshness-summary-card__value">${fmt(latest.total_contacts)}</div>
          <div class="freshness-summary-card__label">Total Contacts</div>
        </div>
        <div class="freshness-summary-card freshness-summary-card--success">
          <div class="freshness-summary-card__value">${fmt(latest.contacts_with_gclid)}</div>
          <div class="freshness-summary-card__label">With GCLID</div>
        </div>
        <div class="freshness-summary-card freshness-summary-card--unknown">
          <div class="freshness-summary-card__value">${fmt(latest.contacts_without_gclid)}</div>
          <div class="freshness-summary-card__label">Without GCLID</div>
        </div>
        <div class="freshness-summary-card freshness-summary-card--running">
          <div class="freshness-summary-card__value">${latest.coverage_pct != null ? escapeHtml(latest.coverage_pct.toFixed(1)) + "%" : "—"}</div>
          <div class="freshness-summary-card__label">Coverage</div>
        </div>
      </div>
      <p style="font-size:12px;color:var(--text-muted);margin-top:var(--space-3)">
        Snapshot date: ${fmt(latest.snapshot_date)}
      </p>`;
  } catch (_) {
    // If endpoint missing or any error, silently hide the panel
    if (panel) panel.hidden = true;
  }
}

// ── Init ───────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", async () => {
  // Wire up login form
  const loginForm = document.getElementById("login-form");
  if (loginForm) loginForm.addEventListener("submit", handleLogin);

  // Wire up sign out button
  const signoutBtn = document.getElementById("btn-signout");
  if (signoutBtn) signoutBtn.addEventListener("click", handleLogout);

  // Wire up sidebar nav items
  document.querySelectorAll(".nav-item[data-page]").forEach((el) => {
    el.addEventListener("click", (e) => {
      e.preventDefault();
      navigate(el.dataset.page, { updateHash: true });
    });
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        navigate(el.dataset.page, { updateHash: true });
      }
    });
  });

  // Wire up the section tab strips (PR-ADS-174A). They are links, so Enter
  // activates them natively; the click is routed through navigate().
  document.querySelectorAll(".section-tab[data-page]").forEach((el) => {
    el.addEventListener("click", (e) => {
      e.preventDefault();
      navigate(el.dataset.page, { updateHash: true });
    });
  });

  // Hash routing: listen for back/forward navigation
  window.addEventListener("hashchange", () => {
    const page = hashToPage(window.location.hash);
    // PR-ADS-124: assert chrome from the hash before navigate runs its loaders.
    applyPageChrome(page);
    navigate(page, { updateHash: false });
  });

  // Wire up Page Help Drawer (PR-ADS-071)
  const helpBtn = document.getElementById("page-help-btn");
  if (helpBtn) {
    helpBtn.addEventListener("click", () => {
      const pageKey = helpBtn.dataset.page || _currentPage;
      if (pageKey) openHelpDrawer(pageKey);
    });
  }
  const helpCloseBtn = document.getElementById("help-drawer-close-btn");
  if (helpCloseBtn) {
    helpCloseBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      closeHelpDrawer();
    });
  }
  const helpOverlay = document.getElementById("help-drawer-overlay");
  if (helpOverlay) {
    helpOverlay.addEventListener("click", (e) => {
      if (e.target === helpOverlay) closeHelpDrawer();
    });
  }

  // Wire up time range buttons
  document.querySelectorAll(".time-range-btn").forEach((btn) => {
    btn.addEventListener("click", () => setSelectedDays(parseInt(btn.dataset.days, 10)));
  });
  // Sync active state to the stored/default value on page load
  updateWindowControls(_selectedDays);

  // PR-ADS-141: wire the Evidence window dropdown (Platform Evidence + Lead
  // Intelligence pages). Populated + shown by applyPageChrome on those pages.
  renderEvidenceWindowDropdown();
  const evidenceSelect = document.getElementById("evidence-window-select");
  if (evidenceSelect) evidenceSelect.addEventListener("change", handleEvidenceWindowSelectChange);

  // Wire up waste filter controls

  // Search Terms + Patterns (PR-ADS-144) controls are wired per-render
  // inside the page module — nothing to bind at startup.

  // Wire up GCLID attribution controls
  const gclidInput      = document.getElementById("gclid-filter-gclid");
  const gclidCampaign   = document.getElementById("gclid-filter-campaign");
  const gclidContact    = document.getElementById("gclid-filter-contact");
  const gclidDeal       = document.getElementById("gclid-filter-deal");
  const gclidStatusSel  = document.getElementById("gclid-filter-status");
  const gclidApplyBtn   = document.getElementById("gclid-apply-btn");
  const gclidClearBtn   = document.getElementById("gclid-clear-btn");
  const gclidRefreshBtn = document.getElementById("gclid-refresh-btn");
  const gclidLoadMoreBtn = document.getElementById("gclid-load-more-btn");

  if (gclidApplyBtn) gclidApplyBtn.addEventListener("click", () => {
    loadGclidAttribution({ reset: true });
    loadGclidQuality();
  });
  if (gclidClearBtn) gclidClearBtn.addEventListener("click", () => {
    if (gclidInput) gclidInput.value = "";
    if (gclidCampaign) gclidCampaign.value = "";
    if (gclidContact) gclidContact.value = "";
    if (gclidDeal) gclidDeal.value = "";
    if (gclidStatusSel) gclidStatusSel.value = "";
    loadGclidAttribution({ reset: true });
    loadGclidQuality();
  });
  if (gclidRefreshBtn) gclidRefreshBtn.addEventListener("click", () => {
    loadGclidAttribution({ reset: true });
    loadGclidCoverageForAttribution();
    loadGclidQuality();
  });
  if (gclidLoadMoreBtn) {
    gclidLoadMoreBtn.addEventListener("click", () => loadGclidAttribution({ reset: false, cursor: gclidNextCursor }));
  }
  [gclidInput, gclidCampaign, gclidContact, gclidDeal].forEach((el) => {
    if (el) el.addEventListener("keydown", (e) => {
      if (e.key === "Enter") loadGclidAttribution({ reset: true });
    });
  });

  // Keyword Evidence (PR-ADS-146) wires its own controls inside the page shell
  // (wireKeywordControls) — no global keyword filter wiring here.

  // Wire up dataset freshness refresh button
  const dfRefreshBtn = document.getElementById("dataset-freshness-refresh-btn");
  if (dfRefreshBtn) dfRefreshBtn.addEventListener("click", loadDatasetFreshness);

  // PR-ADS-191 — CRM Truth Exceptions: refresh and filters re-query the API.
  const cteRefreshBtn = document.getElementById("cte-refresh-btn");
  if (cteRefreshBtn) cteRefreshBtn.addEventListener("click", loadCrmTruthExceptions);
  ["cte-window", "cte-family", "cte-reason"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("change", loadCrmTruthExceptions);
  });
  const cteSearch = document.getElementById("cte-search");
  if (cteSearch) {
    let cteTimer = null;
    cteSearch.addEventListener("input", () => {
      clearTimeout(cteTimer);
      cteTimer = setTimeout(loadCrmTruthExceptions, 300);
    });
  }

  // Wire up backfill form
  const backfillForm = document.getElementById("backfill-form");
  if (backfillForm) backfillForm.addEventListener("submit", runBackfillFromUI);

  // Search Terms tabs (PR-ADS-144) are rendered + wired by the page module.

  // Wire up Executive Overview controls (business windows — PR-ADS-134)
  const dashRefresh = document.getElementById("dashboard-refresh-btn");
  const dashWindow  = document.getElementById("dashboard-window");
  // Refresh reloads whichever dashboard tab is active (PR-ADS-135).
  if (dashRefresh) dashRefresh.addEventListener("click", loadDashboardTab);
  if (dashWindow) {
    dashWindow.value = getRoasBusinessWindow();
    dashWindow.addEventListener("change", handleBusinessWindowSelectChange);
  }

  // Wire up dashboard inner tabs (Overview + Revenue — PR-ADS-135).
  document.querySelectorAll(".dashboard-tab[data-dash-tab]").forEach((btn) => {
    btn.addEventListener("click", () => activateDashboardTab(btn.dataset.dashTab));
  });

  // Wire up the Pipeline page controls (business windows — PR-ADS-178B).
  // The lead filters (Scope, Source) and the list's own filters are wired by
  // leadsRenderControls(); the window select is shared application state, so
  // it is wired here like every other.
  const pipelineRefresh = document.getElementById("pipeline-refresh-btn");
  const pipelineWindow  = document.getElementById("pipeline-window");
  if (pipelineRefresh) pipelineRefresh.addEventListener("click", loadPipeline);
  if (pipelineWindow) {
    pipelineWindow.value = getRoasBusinessWindow();
    pipelineWindow.addEventListener("change", handleBusinessWindowSelectChange);
  }

  // Wire up the Campaigns page controls (business windows — PR-ADS-177B)
  const campaignsRefresh = document.getElementById("campaigns-refresh-btn");
  const campaignsWindow  = document.getElementById("campaigns-window");
  if (campaignsRefresh) campaignsRefresh.addEventListener("click", loadCampaignsPage);
  if (campaignsWindow) {
    campaignsWindow.value = getRoasBusinessWindow();
    campaignsWindow.addEventListener("change", handleBusinessWindowSelectChange);
  }

  // Wire up Country Revenue & Spend controls (business windows — PR-ADS-107A)
  const roasCountryRefresh = document.getElementById("roas-countries-refresh-btn");
  const roasCountryWindow  = document.getElementById("roas-countries-window");
  if (roasCountryRefresh) roasCountryRefresh.addEventListener("click", loadRoasCountries);
  if (roasCountryWindow) {
    roasCountryWindow.value = getRoasBusinessWindow();
    roasCountryWindow.addEventListener("change", handleBusinessWindowSelectChange);
  }


  // Wire up Revenue Attribution Health controls (business windows — PR-ADS-113)
  const revHealthRefresh = document.getElementById("revenue-health-refresh-btn");
  const revHealthWindow  = document.getElementById("revenue-health-window");
  if (revHealthRefresh) revHealthRefresh.addEventListener("click", loadRevenueHealth);
  if (revHealthWindow) {
    revHealthWindow.value = getRoasBusinessWindow();
    revHealthWindow.addEventListener("change", handleBusinessWindowSelectChange);
  }

  // Wire up the System page (PR-ADS-162C)
  const systemRefresh = document.getElementById("system-refresh-btn");
  if (systemRefresh) systemRefresh.addEventListener("click", loadSystem);
  const systemBody = document.getElementById("system-body");
  if (systemBody) systemBody.addEventListener("click", (e) => {
    const el = e.target.closest("[data-system-goto]");
    if (!el) return;
    e.preventDefault();
    navigate(el.dataset.systemGoto, { updateHash: true });
  });

  // Wire up Channels controls (PR-ADS-162A acquisition windows; PR-ADS-181A
  // the period in the hash, column sorting, the channel drawer and the
  // collapsed notes). Every control changes the presentation only.
  const channelsWindow = document.getElementById("channels-window");
  if (channelsWindow) channelsWindow.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-channels-window]");
    if (!btn) return;
    _channelsWindow = btn.dataset.channelsWindow;
    history.replaceState(null, "", channelsHashFor(_channelsWindow));
    loadChannels();
  });
  const channelsBody = document.getElementById("channels-body");
  const openChannel = (row) => {
    // A row of the table being replaced opens nothing: its figures are the
    // previous period's.
    if (channelsBody.classList.contains("is-refreshing")) return;
    _channelsView.drawer = row.dataset.channelOpen;
    renderChannelDrawer();
  };
  if (channelsBody) channelsBody.addEventListener("click", (e) => {
    const sortBtn = e.target.closest("[data-channel-sort]");
    if (sortBtn) {
      const key = sortBtn.dataset.channelSort;
      const on = sortBtn.classList.contains("is-active");
      const was = sortBtn.classList.contains("is-asc") ? "asc" : "desc";
      _channelsView.sort = key;
      _channelsView.dir = on ? (was === "asc" ? "desc" : "asc") : (key === "channel" ? "asc" : "desc");
      renderChannelsFromCache();
      return;
    }
    const more = e.target.closest("[data-channels-expand]");
    if (more) {
      const panel = channelsBody.querySelector(`[data-channels-panel="${more.dataset.channelsExpand}"]`);
      const open = more.getAttribute("aria-expanded") !== "true";
      more.setAttribute("aria-expanded", open ? "true" : "false");
      if (panel) panel.hidden = !open;
      return;
    }
    if (e.target.closest("a, button")) return;
    const row = e.target.closest("[data-channel-open]");
    if (row) openChannel(row);
  });
  if (channelsBody) channelsBody.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const row = e.target.closest("[data-channel-open]");
    if (row && e.target === row) { e.preventDefault(); openChannel(row); }
  });
  const channelsDrawerRoot = document.getElementById("channels-drawer-root");
  if (channelsDrawerRoot) channelsDrawerRoot.addEventListener("click", (e) => {
    if (e.target.closest("[data-channels-drawer-close]")) { closeChannelDrawer(); return; }
    if (e.target.closest("a[href^='#/']")) closeChannelDrawer();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && _channelsView.drawer) closeChannelDrawer();
  });

  // Force-close help drawer on startup (PR-ADS-098: ensures clean state)
  closeHelpDrawer();

  // Check auth and load initial page
  const isAuth = await checkAuth();
  if (isAuth) {
    const initialPage = hashToPage(window.location.hash);
    navigate(initialPage, { updateHash: true, replace: true });
    // Load thresholds in background — do not block first page render.
    loadUiThresholds().then(() => {
      if (_currentPage) loadPage(_currentPage);
    }).catch((err) => {
      console.warn("[loadUiThresholds] background load failed; continuing with defaults.", err);
    });
  }
});

// ── GCLID Readiness & Attribution Confidence (PR-ADS-081/082) ─────────────

async function loadGclidReadiness() {
  const container = document.getElementById("gclid-readiness-summary");
  const blockersEl = document.getElementById("gclid-readiness-blockers");
  const checksEl = document.getElementById("gclid-readiness-next-checks");
  if (!container) return;

  container.innerHTML = '<p class="empty-state">Loading GCLID readiness…</p>';
  if (blockersEl) blockersEl.innerHTML = "";
  if (checksEl) checksEl.innerHTML = "";

  try {
    const data = await fetchJSON(`/api/attribution/gclid-readiness?window=${getCurrentWindowParam()}`);
    if (!data || data.error) {
      container.innerHTML = '<p class="empty-state">Could not load GCLID readiness audit.</p>';
      return;
    }

    const status = data.readiness_status || "UNKNOWN";
    const score = data.readiness_score ?? 0;
    const summary = data.summary || {};

    const statusCls = status === "READY" ? "readiness-status--ready"
      : status === "PARTIAL" ? "readiness-status--partial"
      : status === "NOT_READY" ? "readiness-status--not-ready"
      : "readiness-status--unknown";

    container.innerHTML = `
      <div class="readiness-cards">
        <div class="readiness-card">
          <div class="readiness-card__label">Readiness Status</div>
          <div class="readiness-card__value"><span class="readiness-status-badge ${statusCls}">${escapeHtml(status)}</span></div>
        </div>
        <div class="readiness-card">
          <div class="readiness-card__label">Readiness Score</div>
          <div class="readiness-card__value">${score}/100</div>
        </div>
        <div class="readiness-card">
          <div class="readiness-card__label">Won Deals</div>
          <div class="readiness-card__value">${summary.won_deals_status
            ? canonicalMetricHtml({ value: null, ...summary.won_deals_status })
            : dashValue(summary.won_deals, fmtCount)}</div>
        </div>
        <div class="readiness-card">
          <div class="readiness-card__label">Deals with GCLID</div>
          <div class="readiness-card__value">${summary.won_deals_status
            ? canonicalMetricHtml({ value: null, ...summary.won_deals_status })
            : escapeHtml(String((summary.deals_with_direct_gclid || 0) + (summary.deals_with_contact_gclid || 0)))}</div>
        </div>
        <div class="readiness-card">
          <div class="readiness-card__label">Windsor GCLID Coverage</div>
          <div class="readiness-card__value">${summary.windsor_rows_with_gclid || 0} rows</div>
        </div>
        <div class="readiness-card">
          <div class="readiness-card__label">Exact Matches Possible</div>
          <div class="readiness-card__value">${summary.won_deals_status
            ? canonicalMetricHtml({ value: null, ...summary.won_deals_status })
            : escapeHtml(String(summary.tier_1_possible_matches || 0))}</div>
        </div>
        <div class="readiness-card">
          <div class="readiness-card__label">Source Tag Matches</div>
          <div class="readiness-card__value">${summary.won_deals_status
            ? canonicalMetricHtml({ value: null, ...summary.won_deals_status })
            : escapeHtml(String(summary.tier_2_source_tag_matches || 0))}</div>
        </div>
        <div class="readiness-card">
          <div class="readiness-card__label">Estimated Rows</div>
          <div class="readiness-card__value">${summary.won_deals_status
            ? canonicalMetricHtml({ value: null, ...summary.won_deals_status })
            : escapeHtml(String(summary.tier_3_estimated_required || 0))}</div>
        </div>
      </div>
      <p class="readiness-note">${status === "READY" ? "GCLID bridge is ready for exact attribution." : "GCLID bridge is not fully wired yet. Current campaign revenue attribution remains partly directional."}</p>
    `;

    // Render blockers
    const blockers = data.blockers || [];
    if (blockersEl && blockers.length > 0) {
      blockersEl.innerHTML = `
        <h4 class="readiness-section-title">Blockers</h4>
        <div class="blocker-cards">
          ${blockers.map(b => `
            <div class="blocker-card blocker-card--${escapeHtml(b.severity || "low")}">
              <span class="blocker-severity">${escapeHtml((b.severity || "low").toUpperCase())}</span>
              <span class="blocker-message">${escapeHtml(b.message)}</span>
            </div>
          `).join("")}
        </div>
      `;
    }

    // Render next checks
    const checks = data.next_checks || [];
    if (checksEl && checks.length > 0) {
      checksEl.innerHTML = `
        <h4 class="readiness-section-title">Next Checks</h4>
        <ul class="next-checks-list">
          ${checks.map(c => `<li>${escapeHtml(c)}</li>`).join("")}
        </ul>
      `;
    }
  } catch (err) {
    console.error("[loadGclidReadiness]", err);
    container.innerHTML = '<p class="empty-state">Error loading GCLID readiness.</p>';
  }
}

// ── ROAS & Revenue Pages (PR-ADS-080B) ────────────────────────────────────

const VALID_ROAS_WINDOWS = ["7d", "14d", "30d", "60d", "90d", "365d"];

function getVerdictBadgeClass(verdict) {
  switch (verdict) {
    case "SCALE": return "roas-verdict-badge roas-verdict-badge--scale";
    case "HOLD":  return "roas-verdict-badge roas-verdict-badge--hold";
    case "FIX":   return "roas-verdict-badge roas-verdict-badge--fix";
    case "CUT":   return "roas-verdict-badge roas-verdict-badge--cut";
    case "INSUFFICIENT_DATA": return "roas-verdict-badge roas-verdict-badge--insufficient";
    default: return "roas-verdict-badge";
  }
}

function getAttributionBadge(confidence) {
  switch (confidence) {
    case "tier_1_gclid":          return '<span class="attribution-badge attribution-badge--tier1" title="Click-level match: revenue matched to ad interaction using GCLID evidence" aria-label="Exact GCLID: click-level match">Exact GCLID</span>';
    case "tier_2_source_tag":     return '<span class="attribution-badge attribution-badge--tier2" title="Source tag match: revenue matched using HubSpot paid-search/campaign tags" aria-label="Source Tag: HubSpot paid-search/campaign tag match">Source Tag</span>';
    case "tier_3_spend_weighted": return '<span class="attribution-badge attribution-badge--tier3" title="Estimated: revenue allocated using fallback spend-weighting. Use directionally, not as final truth." aria-label="Estimate: fallback allocation, use directionally">Estimate</span>';
    default: return '<span class="attribution-badge">' + escapeHtml(confidence || "unknown") + '</span>';
  }
}

function fmtRoas(v) {
  if (v === null || v === undefined) return "—";
  return (v * 100).toFixed(0) + "%";
}

function fmtMoney(v) {
  if (v === null || v === undefined) return "—";
  if (Math.abs(v) >= 1000000) return "$" + (v / 1000000).toFixed(1) + "M";
  if (Math.abs(v) >= 1000) return "$" + (v / 1000).toFixed(1) + "k";
  return "$" + v.toFixed(0);
}

// PR-ADS-119 — currency-correct formatting. Native Google Ads spend is GBP and
// must NEVER be rendered with a "$"; USD reporting spend uses "$". A full
// amount (2dp) with an explicit currency suffix is shown for spend-truth values
// that must reconcile to the Google Ads UI / HubSpot.
const _CURRENCY_SYMBOL = { GBP: "£", USD: "$", EUR: "€" };

function currencySymbol(code) {
  return _CURRENCY_SYMBOL[(code || "").toUpperCase()] || "";
}

function fmtCurrency(v, code) {
  if (v === null || v === undefined) return "—";
  const sym = currencySymbol(code);
  const amt = Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${sym}${amt} ${(code || "").toUpperCase()}`.trim();
}

// PR-ADS-123 — compact, currency-correct money for KPI cards / summary strips.
// Native GBP must NEVER render with "$". KPI cards show a compact magnitude
// (e.g. "£69.7k GBP" / "$93.9k USD"); exact full-precision amounts stay in the
// spend proof modal and spend-truth panels via fmtCurrency.
function fmtCompactCurrency(v, code) {
  if (v === null || v === undefined) return "—";
  const sym = currencySymbol(code);
  const cc = (code || "").toUpperCase();
  const abs = Math.abs(v);
  let body;
  if (abs >= 1000000) body = (v / 1000000).toFixed(1) + "M";
  else if (abs >= 1000) body = (v / 1000).toFixed(1) + "k";
  else body = Number(v).toFixed(0);
  return `${sym}${body}${cc ? " " + cc : ""}`;
}

function fmtNativeGBP(v) {
  return fmtCurrency(v, "GBP");
}

function fmtUSD(v) {
  return fmtCurrency(v, "USD");
}

function fmtNum(v, decimals) {
  if (v === null || v === undefined) return "—";
  return Number(v).toFixed(decimals !== undefined ? decimals : 1);
}

function fmtCount(v) {
  if (v === null || v === undefined) return "—";
  return Number(v).toLocaleString();
}

// PR-ADS-180C(-F1): a count the server could not read (None) is "Unknown" —
// never 0, and never a dash a reader can take for "none".
function fmtCountOrUnknown(v) {
  if (v === null || v === undefined) return "Unknown";
  return fmtCount(v);
}

// ── Business revenue windows (PR-ADS-107A) ────────────────────────────────
// Campaign Revenue & Spend / Country Revenue & Spend use business windows (not ad-style
// 7d/14d/30d/60d day windows). Spend = Google Ads API platform evidence;
// revenue = HubSpot closed-won truth. Both pages share one truth contract.
const BUSINESS_WINDOWS = [
  { key: "current_quarter", label: "Current Quarter" },
  { key: "last_quarter",    label: "Last Quarter" },
  { key: "last_6_months",   label: "Last 6 Months" },
  { key: "ytd",             label: "YTD" },
  { key: "all_time",        label: "All Time" },
];
const VALID_BUSINESS_WINDOWS = BUSINESS_WINDOWS.map((w) => w.key);
const DEFAULT_BUSINESS_WINDOW = "current_quarter";

let _roasBusinessWindow = (() => {
  try {
    const stored = sessionStorage.getItem("ads_business_window");
    return VALID_BUSINESS_WINDOWS.includes(stored) ? stored : DEFAULT_BUSINESS_WINDOW;
  } catch (_) {
    return DEFAULT_BUSINESS_WINDOW;
  }
})();

function getRoasBusinessWindow() {
  return VALID_BUSINESS_WINDOWS.includes(_roasBusinessWindow)
    ? _roasBusinessWindow
    : DEFAULT_BUSINESS_WINDOW;
}

function setRoasBusinessWindow(key) {
  const normalized = VALID_BUSINESS_WINDOWS.includes(key) ? key : DEFAULT_BUSINESS_WINDOW;
  _roasBusinessWindow = normalized;
  try { sessionStorage.setItem("ads_business_window", normalized); } catch (_) { /* ignore */ }
  // Keep both ROAS business-window selects in sync.
  document.querySelectorAll("[data-business-window-select]").forEach((sel) => {
    sel.value = normalized;
  });
}

function handleBusinessWindowSelectChange(e) {
  setRoasBusinessWindow(e.target.value);
  switch (_currentPage) {
    case "dashboard":       loadDashboardTab(); break;
    // A new window is a new cohort — restart the leads list at page 1 so the
    // table can never show an offset that belonged to the previous window.
    case "pipeline":        _leadsPage = 1; loadPipeline(); break;
    case "roas-countries":  loadRoasCountries(); break;
    case "revenue-health":  loadRevenueHealth(); break;
    case "campaigns":       loadCampaignsPage(); break;
    default:                break;
  }
}

function getConfidenceBadge(confidence) {
  switch ((confidence || "").toLowerCase()) {
    case "high":   return '<span class="confidence-badge confidence-badge--high" title="High — GCLID / strong attribution">High</span>';
    case "medium": return '<span class="confidence-badge confidence-badge--medium" title="Medium — UTM campaign / clean campaign match">Medium</span>';
    case "low":    return '<span class="confidence-badge confidence-badge--low" title="Low — inferred campaign/country match">Low</span>';
    default:       return '<span class="confidence-badge">' + escapeHtml(confidence || "—") + '</span>';
  }
}

function getBusinessVerdictBadge(verdict, reason) {
  // PR-ADS-177C-F1 (truth audit M-1): several Google Ads campaigns share the
  // row's name — not a spend-mapping gap; its own words.
  if (reason === CAMPAIGN_SHARED_NAME) {
    return '<span class="roas-verdict-badge roas-verdict-badge--mapping" title="Several Google Ads campaigns share this name, so the figures are combined and the row is not judged as one campaign">Not judged — shared name</span>';
  }
  switch ((verdict || "").toLowerCase()) {
    case "winner":   return '<span class="roas-verdict-badge roas-verdict-badge--scale" title="Revenue/customers exist and Period Revenue / Ad Spend (a non-cohort operational ratio) is healthy">Winner</span>';
    case "watch":    return '<span class="roas-verdict-badge roas-verdict-badge--hold" title="SQLs exist but weak customers/revenue">Watch</span>';
    case "waste":    return '<span class="roas-verdict-badge roas-verdict-badge--cut" title="Spend exists but no SQLs/customers/revenue">Waste</span>';
    case "learning": return '<span class="roas-verdict-badge roas-verdict-badge--insufficient" title="Low spend or insufficient data">Learning</span>';
    // PR-ADS-120: an unmapped campaign cannot be judged on incomplete spend.
    case "mapping_required": return '<span class="roas-verdict-badge roas-verdict-badge--mapping" title="Spend mapping required before this campaign can be judged">Mapping required</span>';
    // PR-ADS-161E: the row's closed-won outcome is withheld — it is not judged.
    case "outcome_withheld": return '<span class="roas-verdict-badge roas-verdict-badge--insufficient" title="Closed-won deals for this row are withheld, so it is not judged">Outcome withheld</span>';
    // PR-ADS-176B-F1 (truth audit M1): unknown spend or revenue is not judged —
    // never "watch" / "learning" on a figure nobody measured.
    case "spend_unknown": return '<span class="roas-verdict-badge roas-verdict-badge--insufficient" title="This row&#39;s spend is unknown, so it is not judged">Not judged — spend unknown</span>';
    case "revenue_unknown": return '<span class="roas-verdict-badge roas-verdict-badge--insufficient" title="This row has won deals whose revenue is unknown, so it is not judged">Not judged — revenue unknown</span>';
    // PR-ADS-177A: the row's verdict would depend on its SQL count, which the
    // acquisition cohort's verdict withholds — never "waste" on unknown SQLs.
    case "sqls_unknown": return '<span class="roas-verdict-badge roas-verdict-badge--insufficient" title="This row&#39;s verdict depends on its SQL count, which is withheld, so it is not judged">Not judged — SQLs withheld</span>';
    default:         return '<span class="roas-verdict-badge">' + escapeHtml(verdict || "—") + '</span>';
  }
}

const BUSINESS_VERDICT_ORDER = { winner: 0, watch: 1, waste: 2, learning: 3 };

// PR-ADS-116: every revenue page keeps its OWN window/summary/source-health so a
// Current-Quarter payload can never bleed into another page or another window.
let roasCountryWindow = null;

// Per-page request sequence tokens guard against stale-response races: only the
// newest in-flight request for a page may render. Switching CQ→YTD→Last Quarter
// quickly must never paint an older response over the newest selection.
const _revReqSeq = { campaigns: 0, countries: 0, pipeline: 0, health: 0, overview: 0, dashRevenue: 0, dashCampaigns: 0, dashCountries: 0, dashDeals: 0 };

// PR-ADS-116: show the exact resolved date range beside each window selector.
function formatWindowRange(win) {
  if (!win) return "";
  const start = win.start_date || "Earliest";
  const end = win.end_date || "—";
  return `${start} → ${end}`;
}

// PR-ADS-167A (owner decision 2026-11-20): Country Revenue & Spend and
// Dashboard Countries end a window that includes today at YESTERDAY, for
// revenue and spend alike. The backend says so in `window.window_range`
// (through_reason "today_not_settled") with the sentence the page shows;
// this renders it verbatim, beside the range — never a silent shorter window.
function windowThroughNote(win) {
  const wr = win && win.window_range;
  if (!wr || wr.through_reason !== "today_not_settled") return "";
  return wr.disclosure || "";
}

// An open window with no completed day yet (the first day of a quarter or
// year): every metric is unavailable (`no_settled_day_in_window`).
function windowHasNoSettledDay(win) {
  const wr = win && win.window_range;
  return Boolean(wr && wr.through_reason === "today_not_settled" && wr.empty === true);
}

function renderNoSettledDayCard(win) {
  return `
    <div class="revenue-blocked-card revenue-empty-card">
      <div class="revenue-blocked-card__eyebrow">${escapeHtml((win && win.label) || "This window")}</div>
      <h3>No completed day yet</h3>
      <p>${escapeHtml(windowThroughNote(win))}</p>
    </div>`;
}

function renderWindowRange(elId, win) {
  const el = document.getElementById(elId);
  if (!el) return;
  if (!win) { el.textContent = ""; el.removeAttribute("title"); return; }
  const label = win.label ? `${win.label}: ` : "";
  const note = windowThroughNote(win);
  if (windowHasNoSettledDay(win)) {
    // Never a reversed range ("2026-10-01 → 2026-09-30"): the disclosure only.
    el.textContent = `${label}${note}`;
    el.title = "Period in progress — no completed day yet";
    return;
  }
  el.textContent = `${label}${formatWindowRange(win)}${note ? ` · ${note}` : ""}`;
  el.title = win.is_closed_window ? "Completed period"
    : note ? "Period in progress — ends yesterday" : "Period in progress";
}

// True when this response is still the newest for its page AND its resolved
// window still equals the user's current selection. Fails CLOSED: every revenue
// response must carry a resolved window key — an absent key is never valid.
function _revResponseIsCurrent(page, token, data) {
  if (token !== _revReqSeq[page]) return false;
  const respKey = data && data.window && data.window.key;
  return Boolean(respKey) && respKey === getRoasBusinessWindow();
}

// Show "Loading…" in a range chip so a stale range never sits beside a freshly
// changed selector while the new request is in flight.
function setWindowRangeLoading(elId) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.textContent = "Loading…";
  el.removeAttribute("title");
}


// PR-ADS-110: Campaign Revenue & Spend / Country are business decision pages, not
// diagnostics pages. Decisions are shown only when the truth layer is safe;
// otherwise a single clean blocked card explains why — no debug/audit clutter.
// (Diagnostics live in System Status / Revenue Attribution Health.)
// PR-ADS-115: revenue truth is two independent facts. Readiness requires safe
// lead event dates AND an available canonical closed-won population. A window
// with no closed-won deals is a SAFE EMPTY state (handled downstream), NOT a
// block.
// PR-ADS-161E: the second fact is the CANONICAL verdict. It used to be a
// legacy "revenue integration connected" existence check over the
// gclid_attribution table; a missing or unrecognised status is not available.
function isCanonicalRevenueAvailable(sh) {
  const s = sh || {};
  return s.canonical_revenue_status === "canonical_available";
}

// PR-ADS-177A: the mart's `revenue_decision_ready` no longer waits on the
// leads: they are the acquisition cohort, withheld figure by figure with the
// reason (cohortCountHtml). A withheld cohort blocks no page.
function isRevenueDecisionReady(sourceHealth) {
  const sh = sourceHealth || {};
  return isCanonicalRevenueAvailable(sh);
}

// PR-ADS-177A: whether the acquisition cohort's leads / SQLs are published,
// from the mart's source health (its `lead_metrics_status`).
function cohortLeadsReady(sh) {
  const st = (sh || {}).lead_metrics_status;
  return st !== "withheld" && st !== "unavailable";
}
const COHORT_LEADS_WITHHELD_STEP = " Leads and SQLs are withheld separately: the HubSpot contact-funnel sync has not proven this window's acquisition cohort complete — check the contact-funnel sync in <strong>System Status</strong>.";

function renderRevenueBlockedState(sourceHealth) {
  const sh = sourceHealth || {};
  // PR-ADS-177A: only the revenue side blocks this page; leads / SQLs are the
  // acquisition cohort and are withheld on their own (the legacy lead-grain
  // remedy, "Lead Event-Date Reconciliation", fixed a table no figure reads).
  const leadsReady = cohortLeadsReady(sh);
  const revenueReady = isCanonicalRevenueAvailable(sh);
  const chip = (label, ok, okText, warnText) =>
    `<div class="revenue-status-chip revenue-status-chip--${ok ? "ok" : "warning"}">
       <span>${label}</span><strong>${ok ? okText : warnText}</strong>
     </div>`;
  const nextStep = `<strong>Next step:</strong> The canonical closed-won population is unavailable for this window — check the deal-ledger sync coverage in <strong>System Status</strong>.${leadsReady ? "" : COHORT_LEADS_WITHHELD_STEP}`;
  return `
    <div class="revenue-blocked-card">
      <div class="revenue-blocked-card__eyebrow">Revenue attribution</div>
      <h3>Campaign revenue &amp; spend is not ready yet</h3>
      <p>This page is blocked because the data is not safe enough for business decisions.</p>
      <div class="revenue-status-grid">
        ${chip("Spend", true, "Available", "Available")}
        ${chip("Leads / SQLs", leadsReady, "Available", "Withheld")}
        ${chip("Revenue", revenueReady, "Connected", "Not connected")}
      </div>
      <div class="revenue-next-step">
        ${nextStep}
      </div>
    </div>`;
}

function renderRevenueEmptyState(scope) {
  const grain = scope === "country" ? "countries" : "campaigns";
  return `
    <div class="revenue-blocked-card revenue-empty-card">
      <div class="revenue-blocked-card__eyebrow">Revenue attribution</div>
      <h3>No closed-won revenue found for this window</h3>
      <p>Spend data is available, but no HubSpot closed-won deals are attributed to ${grain} in this business window.</p>
    </div>`;
}

// ── Canonical Revenue Decision Mart client (PR-ADS-126) ─────────────────────
// Every Revenue & Attribution BUSINESS page reads from ONE backend contract:
//   GET /api/revenue-performance?view=campaign|country|source|deal&window=...
// The mart owns the truth — which spend source is trusted, whether FX is
// complete, whether ROAS is safe, whether country geo reconciles, whether a
// campaign is mapped, and whether a source has spend. These pages are renderers
// only: they read summary / spend_truth / readiness / rows / diagnostics and
// never re-derive any of those verdicts. There is NO silent fallback to the old
// page endpoints — a mart failure says so plainly, so page drift cannot return.

// Holds the latest mart response per business page so filter-chip re-renders and
// blocked/ready gating read the SAME canonical payload the loader fetched.
let roasCountryMart = null;
// PR-ADS-130: geo reconciliation root-cause for the blocked Country Revenue & Spend card.
let roasCountryGeoReconcile = null;

async function fetchRevenuePerformance(view, windowKey) {
  const res = await fetch(
    `/api/revenue-performance?view=${encodeURIComponent(view)}&window=${encodeURIComponent(windowKey)}`,
    { credentials: "same-origin" }
  );
  if (!res.ok) return null;
  return res.json();
}

// "verified"/"complete" → Verified, "incomplete" → Incomplete, etc. Never blank.
function labelizeStatus(status) {
  switch ((status || "").toLowerCase()) {
    case "verified":
    case "complete":   return "Verified";
    case "incomplete": return "Incomplete";
    case "mismatch":   return "Mismatch";
    case "unavailable":
    case "":           return "Unavailable";
    default:           return status.charAt(0).toUpperCase() + status.slice(1);
  }
}

// Native (non-USD) spend — reuses the existing currency helper so a GBP amount
// is NEVER rendered with a "$". Unavailable (never $0) when missing.
function fmtNativeMoney(v, currencyCode) {
  if (v === null || v === undefined) return "Unavailable";
  return fmtCurrency(v, currencyCode || "GBP");
}

// Mart-owned readiness. Prefer the backend booleans; the spend_truth shim only
// covers callers without a readiness block.
function martSpendReady(data) {
  if (data && data.readiness) return data.readiness.spend_decision_ready === true;
  const st = (data && data.spend_truth) || {};
  return st.campaign_spend_status === "verified" && st.fx_status === "verified";
}
function martCountryReady(data) {
  if (data && data.readiness) return data.readiness.country_decision_ready === true;
  const st = (data && data.spend_truth) || {};
  return (
    st.campaign_spend_status === "verified" &&
    st.country_spend_status === "verified" &&
    st.fx_status === "verified"
  );
}
function martRoasReady(data, view) {
  if (view === "country") return martCountryReady(data);
  return martSpendReady(data);
}

// A mart fetch failure must NOT silently fall back to the old page endpoints —
// that is exactly the page drift PR-ADS-125/126 removed.
function renderMartUnavailable() {
  return `
    <div class="revenue-blocked-card">
      <div class="revenue-blocked-card__eyebrow">Revenue Decision Mart</div>
      <h3>Canonical revenue mart unavailable</h3>
      <p>This page will not render old page-specific numbers because they may disagree with the mart.</p>
      <div class="revenue-next-step"><strong>Next step:</strong> Check Revenue Health and System Status.</div>
    </div>`;
}

// Feed the existing campaign table/blocked-state renderers from the mart. The
// mart already DECIDED these verdicts; this only maps them to the labels those
// renderers read. The campaign table reads campaign_roas_available (USD vs native
// spend cell) and spend_native_currency off this object.
function martCampaignSourceHealth(data) {
  const st = (data && data.spend_truth) || {};
  const r = (data && data.readiness) || {};
  return {
    campaign_roas_available: martSpendReady(data),
    spend_coverage_status: st.spend_coverage_status,
    fx_coverage_status: st.fx_status === "verified" ? "complete"
      : (st.fx_status === "incomplete" ? "incomplete" : "not_evaluated"),
    spend_native_currency: st.native_currency,
    spend_native_total: st.native_spend,
    spend_usd_total: st.usd_spend,
    lead_date_grain_status: r.lead_date_grain_status,
    lead_metrics_status: r.lead_metrics_status,
    canonical_revenue_status: r.canonical_revenue_status,
    revenue_attribution_status: r.revenue_attribution_status,
    missing_contact_created_at_count: r.missing_contact_created_at_count,
  };
}

// Feed the existing country blocked-state card from the mart readiness block.
function martCountrySourceHealth(data) {
  const r = (data && data.readiness) || {};
  return {
    lead_date_grain_status: r.lead_date_grain_status,
    lead_metrics_status: r.lead_metrics_status,
    canonical_revenue_status: r.canonical_revenue_status,
    revenue_attribution_status: r.revenue_attribution_status,
    missing_contact_created_at_count: r.missing_contact_created_at_count,
    country_spend_available: r.country_spend_available === true,
    geo_country_mapping_status: r.geo_country_mapping_status || "no_geo_data",
  };
}

// ── Campaigns — the one campaign page (PR-ADS-177B) ──────────────────────────
//
// Campaign Evidence (`campaigns`) and Campaign Revenue & Spend (`roas-campaigns`)
// are one page, `campaigns`, built in the Dashboard's visual language (the
// PR-ADS-176B Countries precedent): command bar, KPI cards, campaign table, a
// drawer, the separate "Meta Ads — ad sets" table and a truth footer. It reads
// the mart's SETTLED campaign view
// (`/api/revenue-performance?view=campaign&settled=true`, owner decision
// 2026-11-24, 2): a window that includes today ends yesterday, for revenue and
// spend alike. It decides nothing: the strict spend gate, every total
// (`campaign_totals`), the residual row (`campaign_residual_row`) and every row
// figure come from the server. A figure that is not published reads its
// withholding word with the reason — never "—", never 0. The page sums nothing.

let campaignsPageMart = null;
let campaignsPageMeta = null;
let campaignsPageStatus = "idle";
// Frontend-only decision filter.
let campaignsPageFilter = "all";
// The campaign rows the table last rendered, in order: a row's drawer opens by
// its index here (data-campaign-page-row).
let campaignsPageRenderedRows = [];
// Bumped by every load: a deal list read for an older response is dropped.
let campaignsDrilldownGen = 0;
let campaignsDrilldownCache = {};
// PR-ADS-177C: a campaign another page asked to open (the search-term drawer's
// "Open Campaigns" link). Consumed by the next render; matched by name.
let campaignsPageOpenName = null;

// Period Revenue / Ad Spend notation: a multiple (2.20x), not a percent.
// "Unavailable" when null. Never called ROAS (PR-ADS-161E).
function fmtPeriodRatio(v) {
  if (v === null || v === undefined) return "Unavailable";
  return v.toFixed(2) + "x";
}

// PR-ADS-120: compact secondary spend-status label under the spend amount.
function spendStatusLabel(state) {
  switch (state) {
    case "mapped_exact":       return '<div class="spend-status spend-status--matched">Matched exactly</div>';
    case "mapped_manual":      return '<div class="spend-status spend-status--matched">Matched (manual)</div>';
    case "verified_zero_spend":return '<div class="spend-status spend-status--zero">Verified zero spend</div>';
    default:                   return "";
  }
}

// PR-ADS-120b: when multiple HubSpot labels map to one Google Ads campaign, the
// canonical campaign is shown once (spend never duplicated) with its aliases.
function campaignAliasHtml(aliases) {
  if (!aliases || !aliases.length) return "";
  return `<div class="campaign-aliases">Aliases: ${escapeHtml(aliases.join("; "))}</div>`;
}

// The residual's non-cohort cells: the campaign view counts campaign-
// attributable deals and campaign spend only, so contacts with no campaign
// label have none of either. Never "—", never 0, never in a total.
const CAMPAIGN_NOT_ATTRIBUTABLE_REASON = "contacts with no campaign label have no campaign spend or campaign deals";
function campaignNotAttributableHtml() {
  return `<span class="dash-unavailable camp-not-attributable" title="${escapeHtml(CAMPAIGN_NOT_ATTRIBUTABLE_REASON)}">Not campaign-attributable</span>`
    + ` <small class="metric-reason">${escapeHtml(CAMPAIGN_NOT_ATTRIBUTABLE_REASON)}</small>`;
}

function campaignsPageWindowKey() {
  return ((campaignsPageMart || {}).window || {}).key || getRoasBusinessWindow();
}

async function loadCampaignsPage() {
  const window_ = getRoasBusinessWindow();
  const token = ++_revReqSeq.campaigns;
  setWindowRangeLoading("campaigns-range");
  campaignsDrilldownCache = {};
  campaignsDrilldownGen += 1;
  campaignsPageStatus = "loading";
  const body = document.getElementById("campaigns-page-body");
  if (body) body.innerHTML = '<p class="empty-state" style="padding:var(--space-5)">Loading campaigns…</p>';
  try {
    const [data, meta] = await Promise.all([
      fetch(`/api/revenue-performance?view=campaign&settled=true&window=${encodeURIComponent(window_)}`,
        { credentials: "same-origin" }).then((r) => (r.ok ? r.json() : null)),
      fetch(`/api/meta-ads/adsets?window=${encodeURIComponent(window_)}`,
        { credentials: "same-origin" }).then((r) => (r.ok ? r.json() : null)).catch(() => null),
    ]);
    if (token !== _revReqSeq.campaigns) return;
    if (!data) {
      campaignsPageMart = null;
      campaignsPageStatus = "error";
      if (body) body.innerHTML = renderMartUnavailable();
      return;
    }
    if (!_revResponseIsCurrent("campaigns", token, data)) return;
    campaignsPageMart = data;
    campaignsPageMeta = meta;
    campaignsPageStatus = (data.rows || []).length ? "ok" : "empty";
    renderWindowRange("campaigns-range", data.window || null);
    renderCampaignsPage();
  } catch (err) {
    console.error("[loadCampaignsPage]", err);
    if (token !== _revReqSeq.campaigns) return;
    campaignsPageMart = null;
    campaignsPageStatus = "error";
    if (body) body.innerHTML = renderMartUnavailable();
  }
}

// Business-value sort: outcomes (won deals, revenue) outrank verdict labels.
function sortCampaignsPageRows(rows) {
  return [...rows].sort((a, b) =>
    (b.closed_won_deals || 0) - (a.closed_won_deals || 0) ||
    (b.won_revenue || 0) - (a.won_revenue || 0) ||
    (b.sqls || 0) - (a.sqls || 0) ||
    (b.spend || 0) - (a.spend || 0)
  );
}

function renderCampaignsPageFilters() {
  const filters = [["all", "All"], ["winner", "Winners"], ["watch", "Watch"],
    ["waste", "Waste"], ["learning", "Learning"]];
  return `
    <div class="revenue-filter-chips" id="campaigns-page-filter-chips">
      ${filters.map(([key, label]) => `
        <button type="button" class="revenue-filter-chip ${campaignsPageFilter === key ? "is-active" : ""}"
          data-campaigns-page-filter="${key}">${label}</button>`).join("")}
    </div>`;
}

// PR-ADS-129: campaign spend-coverage diagnostic. Reads ONLY the mart's coverage
// audit (spend_truth.spend_coverage_detail + status/reason) — never recomputed.
// Explains exactly which period is missing/unverified, or confirms a verified-zero
// earlier period so an available ROAS is understood. Returns "" when coverage is
// verified and spend spans the full window.
function renderMartCampaignCoverage(data) {
  const st = (data && data.spend_truth) || {};
  const detail = st.spend_coverage_detail || {};
  const status = st.campaign_spend_coverage_status;
  const reason = st.campaign_spend_coverage_reason;
  const missing = detail.missing_chunks || [];
  const failed = detail.failed_chunks || [];
  const rng = (c) => `${escapeHtml(c.date_from || "—")} → ${escapeHtml(c.date_to || "—")}`;

  // Verified-zero earlier period → coverage complete, ROAS uses verified spend.
  if (status === "complete" && reason === "verified_zero_before_first_spend") {
    return `
      <div class="revenue-blocked-card revenue-empty-card">
        <div class="revenue-blocked-card__eyebrow">Google Ads spend truth</div>
        <h3>Coverage complete</h3>
        <p>${escapeHtml(detail.requested_start || "—")} → ${escapeHtml(detail.first_spend_date || "—")} was verified as zero spend. Period Revenue / Ad Spend uses verified spend for this window.</p>
      </div>`;
  }

  // Incomplete / unknown coverage → name the exact missing / unverified period.
  if (status === "incomplete" || status === "unknown") {
    const period = failed.length ? rng(failed[0])
      : (missing.length ? rng(missing[0])
        : `${escapeHtml(detail.requested_start || "—")} → ${escapeHtml(detail.first_spend_date || "—")}`);
    const nextStep = failed.length
      ? "Re-run the Google Ads spend backfill for the failed chunks."
      : "Run Google Ads spend backfill for the missing period, or verify this period had zero spend.";
    return `
      <div class="revenue-blocked-card">
        <div class="revenue-blocked-card__eyebrow">Google Ads spend truth</div>
        <h3>Campaign spend coverage incomplete</h3>
        <div class="revenue-summary-strip revenue-summary-strip--three revenue-coverage-grid">
          <div><span>Requested window</span><strong>${escapeHtml(detail.requested_start || "—")} → ${escapeHtml(detail.requested_end || "—")}</strong></div>
          <div><span>Durable spend coverage</span><strong>${escapeHtml(detail.first_spend_date || "—")} → ${escapeHtml(detail.last_spend_date || "—")}</strong></div>
          <div><span>Missing / unverified period</span><strong>${period}</strong></div>
        </div>
        <div class="revenue-next-step"><strong>Next step:</strong> ${nextStep}</div>
      </div>`;
  }
  return "";
}

// The strict spend gate (as on Countries): the mart's `spend_decision_ready`.
// A window whose campaign spend is not held completely — yesterday not yet
// re-read since it ended, a missing or failed range, FX incomplete — shows
// this card and NO figure: never the number with a warning.
function renderCampaignSpendGateState(data) {
  const st = (data && data.spend_truth) || {};
  const code = st.campaign_spend_unsettled_reason
    || (st.fx_status !== "verified" && st.spend_coverage_status === "complete" ? "fx_coverage_incomplete" : null)
    || st.campaign_spend_coverage_reason || "google_ads_spend_coverage_or_fx_unverified";
  const why = canonicalReasonLabel(code) || "campaign spend is not verified for this window";
  const next = COUNTRY_GAP_NEXT_STEP[code]
    || "Open Revenue Health and review Google Ads spend coverage and FX for this window.";
  const chip = (label, value, ok) =>
    `<div class="revenue-status-chip revenue-status-chip--${ok ? "ok" : "warning"}">
       <span>${label}</span><strong>${escapeHtml(value)}</strong>
     </div>`;
  return `
    <div class="revenue-blocked-card" data-campaign-spend-gate="${escapeHtml(code)}">
      <div class="revenue-blocked-card__eyebrow">Google Ads spend truth</div>
      <h3>Campaign figures are withheld for this window</h3>
      <p>Campaign spend is not verified: ${escapeHtml(why)}. No campaign figure is shown until it is — never a partial spend with a warning.</p>
      <div class="revenue-status-grid">
        ${chip("Campaign spend", labelizeStatus(st.campaign_spend_status), st.campaign_spend_status === "verified")}
        ${chip("FX coverage", labelizeStatus(st.fx_status), st.fx_status === "verified")}
      </div>
      <div class="revenue-next-step"><strong>Next step:</strong> ${escapeHtml(next)}</div>
    </div>
    ${st.campaign_spend_unsettled_reason ? "" : renderMartCampaignCoverage(data)}`;
}

// The Dashboard KPI cards, over the server's `campaign_totals`: the mart's own
// campaign-attributable verdicts, each {value, status, reason}. Leads and SQLs
// are the cohort total — the campaign rows plus the residual row. Customers are
// the canonical distinct companies of the campaign-attributable deals, never a
// sum of rows.
function renderCampaignsPageSummary(data) {
  const t = (data && data.campaign_totals) || {};
  const opts = campBlockerOpts(data);
  const cohort = data && data.cohort;
  const residual = (data && data.campaign_residual_row) || null;
  const through = cohortThroughSuffix(cohort);
  const known = (key) => t[key] !== null && t[key] !== undefined;
  const residualNote = (field) => residual && cohortCountPublication(cohort).publish
    && residual[field] !== null && residual[field] !== undefined
    ? ` · incl. ${fmtCount(residual[field])} with no campaign label` : "";
  const cards = [
    { key: "spend", label: "Campaign Spend", value: rowMetricHtml(t, "spend", fmtMoney, opts),
      sub: "USD · Google Ads campaign spend · settled days", source: "Google Ads API", ok: known("spend") },
    { key: "leads", label: COHORT_LEADS_LABEL, value: cohortCountHtml(t.leads, cohort, null),
      sub: `Google Ads contacts${residualNote("leads")}${through}`, source: "HubSpot acquisition cohort",
      ok: cohortCountPublication(cohort).publish && known("leads") },
    { key: "sqls", label: COHORT_SQLS_LABEL, value: cohortCountHtml(t.sqls, cohort, null),
      sub: `Google Ads contacts${residualNote("sqls")}${through}`, source: "HubSpot acquisition cohort",
      ok: cohortCountPublication(cohort).publish && known("sqls") },
    { key: "deals", label: "Won Deals", value: rowMetricHtml(t, "closed_won_deals", fmtCount, opts),
      sub: "Campaign-attributable · closed this window", source: "HubSpot Closed-Won", ok: known("closed_won_deals") },
    { key: "customers", label: "Customers", value: rowMetricHtml(t, "customers", fmtCount, opts),
      sub: "Distinct companies · not a sum of the rows", source: "HubSpot companies on closed-won deals", ok: known("customers") },
    { key: "revenue", label: "Won Revenue", value: rowMetricHtml(t, "won_revenue", fmtMoney, opts),
      sub: "USD · campaign-attributable", source: "HubSpot Closed-Won", ok: known("won_revenue") },
    { key: "period_revenue_per_ad_spend", label: PERIOD_RATIO_LABEL,
      value: rowMetricHtml(t, "period_revenue_per_ad_spend", fmtPeriodRatio, opts),
      // The value carries the non-cohort disclosure (canonicalMetricHtml).
      sub: "Campaign-attributable won revenue ÷ campaign spend · same settled days",
      source: "Revenue closed this window over the same window's spend",
      ok: known("period_revenue_per_ad_spend") },
  ];
  return `
    <div class="dash-kpi-grid camp-page-kpi-grid">
      ${cards.map((c) => `
        <div class="dash-kpi-card dash-anim ${c.ok ? "" : "dash-kpi-card--unavailable"}" data-kpi="${c.key}">
          <div class="dash-kpi-card__label">${escapeHtml(c.label)}</div>
          <div class="dash-kpi-card__value">${c.value}</div>
          <div class="dash-kpi-card__sub">${escapeHtml(c.sub)}</div>
          <div class="dash-kpi-card__source">${escapeHtml(c.source)}</div>
        </div>`).join("")}
    </div>`;
}

function campaignsPageSpendCell(r) {
  if (r.spend_state === "unmapped" || r.spend_mapping === "unavailable") {
    return '<span class="dash-unavailable" title="Spend mapping required">Unavailable</span>'
      + ' <small class="metric-reason">spend mapping required</small>';
  }
  if (r.spend === null || r.spend === undefined) {
    return '<span class="dash-unavailable">Unavailable</span> <small class="metric-reason">campaign spend is not verified</small>';
  }
  return `${escapeHtml(fmtMoney(r.spend))}${spendStatusLabel(r.spend_state)}`;
}

function renderCampaignsResidualRow(res, cohort) {
  if (!res) return "";
  return `
    <tr class="camp-row camp-row--residual revenue-residual-row" aria-label="${escapeHtml(res.label || "No campaign label")} (not a campaign)">
      <td class="camp-td camp-td--name"><span class="camp-dot camp-dot--muted"></span><span class="revenue-residual-name">${escapeHtml(res.label || "No campaign label")}</span> <span class="revenue-residual-badge">Residual</span>
        <div class="camp-residual-def">${escapeHtml(res.definition || "")}</div></td>
      <td class="camp-num">${campaignNotAttributableHtml()}</td>
      <td class="camp-num">${cohortCountHtml(res.leads, cohort, res)}</td>
      <td class="camp-num">${cohortCountHtml(res.sqls, cohort, res)}</td>
      <td class="camp-num">${campaignNotAttributableHtml()}</td>
      <td class="camp-num">${campaignNotAttributableHtml()}</td>
      <td class="camp-num">${campaignNotAttributableHtml()}</td>
      <td>${campaignNotAttributableHtml()}</td>
      <td><span class="decision-badge decision-badge--unattributed">Not a campaign</span></td>
    </tr>`;
}

function renderCampaignsPageTable(rows, data) {
  const opts = campBlockerOpts(data);
  const cohort = opts.cohort;
  const through = cohortThroughSuffix(cohort);
  const head = `
    <tr>
      <th>Campaign</th>
      <th>Spend</th>
      <th title="${escapeHtml(COHORT_COUNT_TITLE)}">Leads<span class="th-sub"> · contacts acquired in window${escapeHtml(through)}</span></th>
      <th title="${escapeHtml(COHORT_COUNT_TITLE)}">SQLs<span class="th-sub"> · contacts acquired in window${escapeHtml(through)}</span></th>
      <th>Won Deals</th>
      <th title="Distinct proven HubSpot companies — not additive across campaigns">Customers</th>
      <th>Won Revenue</th>
      <th title="${escapeHtml(PERIOD_RATIO_DISCLOSURE)}">${PERIOD_RATIO_LABEL}</th>
      <th>Status</th>
    </tr>`;
  const body = rows.length ? rows.map((r, i) => `
    <tr class="camp-row" data-campaign-page-row="${i}" tabindex="0" role="button" aria-label="${escapeHtml(`Open ${r.campaign_name || "campaign"} detail`)}">
      <td class="camp-td camp-td--name">${escapeHtml(r.campaign_name || "")}${campaignAliasHtml(r.aliases)}${campaignSharedNameHtml(r)}</td>
      <td class="camp-num">${campaignsPageSpendCell(r)}</td>
      <td class="camp-num">${cohortCountHtml(r.leads, cohort, r)}</td>
      <td class="camp-num">${cohortCountHtml(r.sqls, cohort, r)}</td>
      <td class="camp-num">${rowMetricHtml(r, "closed_won_deals", fmtCount, opts)}</td>
      <td class="camp-num">${rowMetricHtml(r, "customers", fmtCount, opts)}</td>
      <td class="camp-num">${rowMetricHtml(r, "won_revenue", fmtMoney, opts)}</td>
      <td>${rowMetricHtml(r, "period_revenue_per_ad_spend", fmtPeriodRatio, opts)}${campaignSharedRatioNote(r)}</td>
      <td>${getBusinessVerdictBadge(r.verdict, r.verdict_reason)}</td>
    </tr>`).join("")
    : `<tr><td colspan="9" class="rev-breakdown-empty">No campaigns match this decision filter.</td></tr>`;
  return `
    <div class="camp-scroll">
      <table class="camp-table camp-page-table">
        <thead>${head}</thead>
        <tbody>${body}${renderCampaignsResidualRow((data || {}).campaign_residual_row, cohort)}</tbody>
      </table>
    </div>`;
}

// PR-ADS-153E-B: an ADVERTISING view must say which population its revenue was
// measured over, and must not read as the company total. The mart carries both:
// `summary.revenue_scope` is the all-source business scope, and
// `summary.attributed_revenue_scope` is the narrower scope the period ratio's
// numerator used. Showing them together stops attributed revenue being mistaken
// for total business revenue (kept by PR-ADS-177B on the one Campaigns page).
// PR-ADS-177C (M6): plain words for the two revenue scopes, never the raw
// keys, and the canonical verdict for a total that is not published —
// "Withheld" with its reason for a withheld total (unpriced deals …),
// "Unavailable" only when the ledger could not be read.
const REVENUE_SCOPE_WORDS = {
  "campaign_attributable": "revenue from closed-won deals placed on a Google Ads campaign",
  "google_ads_source": "revenue from closed-won deals whose contact came from Google Ads",
  "all_source": "closed-won revenue from every source",
};
function revenueScopeWords(key) {
  return REVENUE_SCOPE_WORDS[key] || "closed-won revenue in this scope";
}
function revenueScopeAmountHtml(value, metric) {
  if (value !== null && value !== undefined) return escapeHtml(fmtMoney(value));
  const m = metric || {};
  const word = m.status === "withheld" ? "Withheld" : "Unavailable";
  const why = m.reason ? canonicalReasonLabel(m.reason) : "";
  return `<strong class="dash-unavailable">${word}</strong>`
    + (why ? ` <small class="metric-reason">(${escapeHtml(why)})</small>` : "");
}
function renderRevenueScopeDisclosure(data) {
  const summary = (data && data.summary) || {};
  const attributed = summary.attributed_revenue_scope;
  if (!attributed) return "";
  const business = summary.revenue_scope || "all_source";
  const metrics = summary.metrics || {};
  return `<p class="grace-note" data-revenue-scope="${escapeHtml(attributed)}">
      Revenue on this page is ${escapeHtml(revenueScopeWords(attributed))}:
      ${revenueScopeAmountHtml(summary.attributed_won_revenue_usd, metrics.attributed_revenue_usd)}
      this window, out of ${revenueScopeAmountHtml(summary.won_revenue_usd, metrics.revenue_usd)}
      of ${escapeHtml(revenueScopeWords(business))}. The campaign figure is a subset of
      total business revenue, not the same figure.
    </p>`;
}

function renderCampaignsTruthFooter(data) {
  const st = (data && data.spend_truth) || {};
  const ok = (v) => v === "verified" || v === "complete";
  const chip = (label, status) => `
    <span class="dash-footer-chip" title="${escapeHtml(`${label}: ${labelizeStatus(status)}`)}">
      <span class="dash-dot ${ok(status) ? "dash-dot--ok" : "dash-dot--warn"}"></span>${escapeHtml(label)} · ${escapeHtml(labelizeStatus(status))}
    </span>`;
  return `
    <footer class="dash-truth-footer">
      ${chip("Campaign spend (settled days)", st.campaign_spend_status)}
      ${chip("FX", st.fx_status)}
      <span class="dash-footer-chip">Revenue: HubSpot closed-won · campaign-attributable</span>
      <span class="dash-footer-chip">Meta ad sets: separate table · never summed with Google Ads</span>
    </footer>`;
}

function renderCampaignsSafeEmptyState() {
  return `
    <div class="revenue-blocked-card revenue-empty-card">
      <div class="revenue-blocked-card__eyebrow">Campaigns</div>
      <h3>No campaign activity in this window</h3>
      <p>Google Ads spend and HubSpot are ready, but no campaign has spend, contacts or closed-won deals in this business window.</p>
    </div>`;
}

function renderCampaignsPage() {
  const body = document.getElementById("campaigns-page-body");
  if (!body) return;
  campaignsPageRenderedRows = [];
  const data = campaignsPageMart;
  if (!data) { body.innerHTML = renderMartUnavailable(); return; }
  // PR-ADS-167A: no completed day in the window yet — nothing was measured, so
  // no card and no table: the disclosure only (the Meta table too).
  if (windowHasNoSettledDay(data.window)) {
    body.innerHTML = renderNoSettledDayCard(data.window);
    return;
  }
  const meta = renderMetaAdsetPanel(campaignsPageMeta);
  if (!data.readiness || data.readiness.revenue_decision_ready !== true) {
    body.innerHTML = renderRevenueBlockedState(martCampaignSourceHealth(data)) + meta;
    return;
  }
  if (data.readiness.spend_decision_ready !== true) {
    body.innerHTML = renderCampaignSpendGateState(data) + meta;
    return;
  }
  const rows = sortCampaignsPageRows((data.rows || []).filter((r) => r && !r.is_residual));
  // Ready, and nothing in this window: no campaign and no contact on the
  // residual. A safe empty state — not a block, and not a table of zeros.
  if (!rows.length && !data.campaign_residual_row) {
    body.innerHTML = renderCampaignsSafeEmptyState() + meta;
    return;
  }
  const filtered = campaignsPageFilter === "all" ? rows
    : rows.filter((r) => r.verdict === campaignsPageFilter);
  campaignsPageRenderedRows = filtered;
  body.innerHTML = `
    ${renderMartCampaignCoverage(data)}
    ${renderCampaignsPageSummary(data)}
    ${martCohortFunnelHtml(data.cohort, { title: "Lifecycle funnel",
      sub: "Google Ads-sourced HubSpot contacts created in this window — the Leads above — and how many have reached each stage so far" })}
    <section class="dash-panel dash-anim camp-leaderboard-panel camp-page-panel" aria-label="Google Ads campaigns">
      <div class="dash-panel__header">
        <div>
          <h3 class="dash-panel__title">Google Ads campaigns</h3>
          <p class="dash-panel__sub">Every campaign with spend, contacts or closed-won deals, then the contacts with no campaign label · click a campaign for its closed-won deals · ${escapeHtml(PERIOD_RATIO_LABEL)}: ${escapeHtml(PERIOD_RATIO_DISCLOSURE)}</p>
        </div>
      </div>
      ${renderCampaignsPageFilters()}
      ${renderCampaignsPageTable(filtered, data)}
      ${renderRevenueScopeDisclosure(data)}
    </section>
    ${meta}
    ${renderCampaignsTruthFooter(data)}`;
  if (campaignsPageOpenName) {
    const want = String(campaignsPageOpenName).trim().toLowerCase();
    campaignsPageOpenName = null;
    const idx = filtered.findIndex((r) => String(r.campaign_name || "").trim().toLowerCase() === want);
    if (idx >= 0) openCampaignsPageDrawer(idx);
  }
}

// ── The drawer: the row's figures, then the closed-won deals behind it ──────
// Read lazily from `/api/revenue-performance/campaign-detail?settled=true`, the
// row's own population over the same settled days. Its deal ids must EQUAL the
// row's `deal_ids`: a list that differs is not shown (fail closed). A row whose
// deal count is withheld reads nothing and says so.
function renderCampaignDrilldown(data, row) {
  const status = (data && data.source_health && data.source_health.status) || "available";
  if (status === "database_unavailable" || (data && data.revenue_available === false)) {
    return `<p class="revenue-footnote">Client/deal detail is unavailable${data && data.revenue_unavailable_reason ? ` — ${escapeHtml(canonicalReasonLabel(data.revenue_unavailable_reason))}` : ""}.</p>`;
  }
  const details = (data && data.details) || [];
  const want = (row && Array.isArray(row.deal_ids)) ? [...row.deal_ids].map(String).sort() : null;
  const got = details.map((d) => String(d.deal_record_id)).filter((v) => v && v !== "null").sort();
  if (want && JSON.stringify(want) !== JSON.stringify(got)) {
    return `<p class="dash-unavailable">The deal list does not match this row's ${escapeHtml(fmtCount(want.length))} closed-won deal(s), so it is not shown.</p>`;
  }
  if (!details.length) {
    return `<p class="revenue-footnote">No closed-won deals for this campaign in this window.</p>`;
  }
  const header = `
    <tr>
      <th>Company</th><th>Company ID</th><th>Main Contact</th><th>Contact ID</th>
      <th>Deal</th><th>Deal ID</th><th>Amount</th><th>Close Date</th><th>Attribution</th>
    </tr>`;
  const body = details.map((d) => `
    <tr>
      <td>${drilldownValue(d.company_name)}</td>
      <td>${drilldownValue(d.company_record_id)}</td>
      <td>${drilldownValue(d.main_contact_name)}</td>
      <td>${drilldownValue(d.main_contact_record_id)}</td>
      <td>${drilldownValue(d.deal_name)}</td>
      <td>${drilldownValue(d.deal_record_id)}</td>
      <td>${d.deal_amount_usd == null ? drilldownValue(null) : fmtMoney(d.deal_amount_usd)}</td>
      <td>${drilldownValue(d.deal_close_date)}</td>
      <td>${drilldownValue(d.match_method)}</td>
    </tr>`).join("");
  return `
    <div class="campaign-drilldown__title">Closed-won deals behind this campaign</div>
    <div class="table-scroll campaign-drilldown__scroll">
      <table class="data-table revenue-decision-table campaign-drilldown-table">
        ${header}
        <tbody>${body}</tbody>
      </table>
    </div>`;
}

// Format a possibly-missing value: real value or an explicit "unavailable" chip.
function drilldownValue(v) {
  if (v === null || v === undefined || v === "") {
    return '<span class="detail-unavailable">unavailable</span>';
  }
  return escapeHtml(String(v));
}

function renderCampaignsPageDrawer(r, data) {
  const opts = campBlockerOpts(data);
  const cohort = (data || {}).cohort;
  // PR-ADS-177C: one Keywords & Search Terms page. PR-ADS-178B: Deals is a
  // section of the one Pipeline page — the link names it, as on the Countries
  // drawers (docs/61 §7 left this one to 177C).
  const links = [["pipeline", "Open Pipeline"],
    ["search-terms", "Open Keywords & Search Terms"], ["revenue-health", "Open Revenue Health"]]
    .filter(([page]) => dashCanNavigate(page))
    .map(([page, label]) => `<a class="camp-drawer__link" href="#/${escapeHtml(page)}">${escapeHtml(label)} →</a>`).join("");
  const dealsWithheld = r.closed_won_deals === null || r.closed_won_deals === undefined;
  return `
    <div class="camp-drawer__head">
      <strong>${escapeHtml(r.campaign_name || "Campaign")}</strong>
      ${getBusinessVerdictBadge(r.verdict, r.verdict_reason)}
      ${r.campaign_id ? spendProofButton(r.campaign_id, r.campaign_name) : ""}
      <button type="button" class="camp-drawer__close" data-campaigns-drawer-close aria-label="Close">×</button>
    </div>
    <dl class="camp-drawer__summary">
      <div><dt>Spend</dt><dd>${campaignsPageSpendCell(r)}</dd></div>
      <div><dt>${escapeHtml(COHORT_LEADS_LABEL + cohortThroughSuffix(cohort))}</dt><dd>${cohortCountHtml(r.leads, cohort, r)}</dd></div>
      <div><dt>${escapeHtml(COHORT_SQLS_LABEL + cohortThroughSuffix(cohort))}</dt><dd>${cohortCountHtml(r.sqls, cohort, r)}</dd></div>
      <div><dt>Won deals</dt><dd>${rowMetricHtml(r, "closed_won_deals", fmtCount, opts)}</dd></div>
      <div><dt>Customers</dt><dd>${rowMetricHtml(r, "customers", fmtCount, opts)}</dd></div>
      <div><dt>Won revenue</dt><dd>${rowMetricHtml(r, "won_revenue", fmtMoney, opts)}</dd></div>
      <div><dt>${escapeHtml(PERIOD_RATIO_LABEL)}</dt><dd>${rowMetricHtml(r, "period_revenue_per_ad_spend", fmtPeriodRatio, opts)}${campaignSharedRatioNote(r)}</dd></div>
    </dl>
    ${campaignSharedIdsHtml(r)}
    <div class="camp-drawer__section">
      <div class="campaign-drilldown">${dealsWithheld
        ? `<p class="dash-unavailable">Closed-won deals are withheld for this campaign in this window.</p>`
        : ""}</div>
    </div>
    <div class="camp-drawer__actions">${links}</div>`;
}

async function loadCampaignsPageDrilldown(r, drawer, windowKey) {
  const container = drawer.querySelector(".campaign-drilldown");
  if (!container) return;
  const gen = campaignsDrilldownGen;
  const key = `${windowKey}:${r.campaign_name || ""}`;
  if (campaignsDrilldownCache[key]) {
    container.innerHTML = renderCampaignDrilldown(campaignsDrilldownCache[key], r);
    return;
  }
  container.innerHTML = '<p class="revenue-footnote">Loading closed-won deals…</p>';
  try {
    const res = await fetch(
      `/api/revenue-performance/campaign-detail?settled=true&window=${encodeURIComponent(windowKey)}&campaign=${encodeURIComponent(r.campaign_name || "")}`,
      { credentials: "same-origin" });
    if (!res.ok) {
      container.innerHTML = `<p class="revenue-footnote">Could not load the closed-won deals for this campaign.</p>`;
      return;
    }
    const data = await res.json();
    if (gen !== campaignsDrilldownGen) return;   // the page reloaded meanwhile
    campaignsDrilldownCache[key] = data;
    container.innerHTML = renderCampaignDrilldown(data, r);
  } catch (err) {
    console.error("[loadCampaignsPageDrilldown]", err);
    container.innerHTML = `<p class="revenue-footnote">Error loading the closed-won deals.</p>`;
  }
}

function openCampaignsPageDrawer(idx) {
  const r = campaignsPageRenderedRows[idx];
  const panel = document.querySelector("#campaigns-page-body .camp-page-panel");
  if (!r || !panel) return;
  let drawer = panel.querySelector(".camp-drawer");
  if (!drawer) {
    drawer = document.createElement("div");
    drawer.className = "camp-drawer";
    panel.appendChild(drawer);
  }
  const data = campaignsPageMart;
  drawer.innerHTML = renderCampaignsPageDrawer(r, data);
  if (r.closed_won_deals !== null && r.closed_won_deals !== undefined) {
    loadCampaignsPageDrilldown(r, drawer, ((data || {}).window || {}).key || getRoasBusinessWindow());
  }
  if (drawer.scrollIntoView) drawer.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

document.addEventListener("click", (e) => {
  const close = e.target.closest("[data-campaigns-drawer-close]");
  if (close) {
    const drawer = close.closest(".camp-drawer");
    if (drawer) drawer.remove();
    return;
  }
  const chip = e.target.closest("[data-campaigns-page-filter]");
  if (chip) {
    campaignsPageFilter = chip.dataset.campaignsPageFilter || "all";
    renderCampaignsPage();
    return;
  }
  const row = e.target.closest("#campaigns-page-body [data-campaign-page-row]");
  if (!row || e.target.closest("a") || e.target.closest("button")) return;
  openCampaignsPageDrawer(Number(row.getAttribute("data-campaign-page-row")));
});

document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const row = e.target.closest && e.target.closest("#campaigns-page-body [data-campaign-page-row]");
  if (!row) return;
  e.preventDefault();
  openCampaignsPageDrawer(Number(row.getAttribute("data-campaign-page-row")));
});

// ── Meta Ads — ad sets (owner decision 2026-11-24, 4) ───────────────────────
// A separate table: each Meta ad set is its own row, with its own totals,
// never summed with a Google Ads figure. Spend in the account's currency and
// USD, impressions, clicks and Meta-reported leads (Meta's own attributed
// count — not contacts, not CRM leads). The SQL, deal and revenue cells read
// "Not linked yet": no ad set is joined to HubSpot. The ONE gate every figure
// goes through: a table that is not published, or rests on the owner's
// retirement declaration without its disclosure, shows no figure.
const META_ADSET_NOT_LINKED_TEXT = "No Meta ad set is joined to a HubSpot contact or deal yet";
// PR-ADS-177C (M7): said on EVERY state of the table — published, withheld or
// unreadable — because the Google Ads table above it counts London days. The
// server's sentence names each account's zone; this is its fallback.
const META_ADSET_DAY_BASIS_TEXT = "Each Meta ad account is read on its own calendar days, in its own time zone. These are not the Europe/London days the Google Ads table counts on; nothing on this page is divided by Meta spend.";
function metaAdsetDayBasisHtml(data) {
  const text = (data && data.day_basis_disclosure) || META_ADSET_DAY_BASIS_TEXT;
  return `<p class="camp-keyword-note meta-adset-day-basis">${escapeHtml(text)}</p>`;
}
function metaAdsetTableGate(data) {
  if (!data) return { ok: false, reason: "meta_adset_source_unavailable", detail: null };
  if (data.status !== "published") {
    return { ok: false, reason: data.reason || "meta_adset_source_unavailable", detail: data.detail || null };
  }
  if ((data.owner_declared_retired || []).length && !data.owner_declared_retired_disclosure) {
    return { ok: false, reason: "meta_retired_disclosure_missing", detail: null };
  }
  return { ok: true, reason: null, detail: null };
}

function metaFigureHtml(m, formatter) {
  const x = m || {};
  if (x.status === "published" && x.value !== null && x.value !== undefined) {
    return escapeHtml(formatter(x.value, x.currency));
  }
  const why = canonicalReasonLabel(x.reason);
  return `<span class="dash-unavailable" title="${escapeHtml(why)}">Unavailable</span>${why ? ` <small class="metric-reason">${escapeHtml(why)}</small>` : ""}`;
}

function metaNotLinkedHtml() {
  return `<span class="dash-unavailable meta-not-linked" title="${escapeHtml(META_ADSET_NOT_LINKED_TEXT)}">Not linked yet</span>`;
}

function renderMetaAdsetTable(data) {
  const gate = metaAdsetTableGate(data);
  if (!gate.ok) {
    const why = canonicalReasonLabel(gate.reason) || "the ad-set figures are not verified";
    return `<p class="dash-unavailable meta-adset-withheld">${data && data.status === "unavailable" ? "Unavailable" : "Withheld"} — ${escapeHtml(why)}</p>`
      + (gate.detail ? `<p class="camp-keyword-note">${escapeHtml(gate.detail)}</p>` : "");
  }
  // Full amounts with the currency code: Meta spend is small, and a compact
  // "$0" or "$6.0k" would hide or round what the ledger holds.
  const native = (v, cur) => fmtCurrency(v, cur || "");
  const usd = (v) => fmtCurrency(v, "USD");
  const count = (v) => fmtCount(v);
  const rows = data.rows || [];
  const head = ["Ad set", "Spend (account currency)", "Spend (USD)", "Impressions", "Clicks",
    "Meta-reported leads", "SQLs", "Won deals", "Won revenue"].map((h) => `<th>${escapeHtml(h)}</th>`).join("");
  const body = rows.map((r) => `
    <tr class="meta-adset-row">
      <td class="camp-td camp-td--name">${escapeHtml(r.adset_name || r.adset_id || "Ad set")}
        <div class="camp-residual-def">${escapeHtml(r.campaign_name || r.campaign_id || "")} · ${escapeHtml(r.account_name || r.account_id || "")}${r.time_zone ? ` · ${escapeHtml(r.time_zone)}` : ""}</div></td>
      <td class="camp-num">${metaFigureHtml(r.spend, native)}</td>
      <td class="camp-num">${metaFigureHtml(r.spend_usd, usd)}</td>
      <td class="camp-num">${metaFigureHtml(r.impressions, count)}</td>
      <td class="camp-num">${metaFigureHtml(r.clicks, count)}</td>
      <td class="camp-num">${metaFigureHtml(r.meta_reported_leads, count)}</td>
      <td class="camp-num">${metaNotLinkedHtml()}</td>
      <td class="camp-num">${metaNotLinkedHtml()}</td>
      <td class="camp-num">${metaNotLinkedHtml()}</td>
    </tr>`).join("") || `<tr><td colspan="9" class="rev-breakdown-empty">No Meta ad set delivered in this window.</td></tr>`;
  const t = data.totals || {};
  const totalRow = rows.length ? `
    <tr class="meta-adset-total">
      <td class="camp-td camp-td--name"><strong>Meta ad sets — total</strong><div class="camp-residual-def">This table only · never added to Google Ads</div></td>
      <td class="camp-num">${metaFigureHtml(t.spend, native)}</td>
      <td class="camp-num">${metaFigureHtml(t.spend_usd, usd)}</td>
      <td class="camp-num">${metaFigureHtml(t.impressions, count)}</td>
      <td class="camp-num">${metaFigureHtml(t.clicks, count)}</td>
      <td class="camp-num">${metaFigureHtml(t.meta_reported_leads, count)}</td>
      <td class="camp-num">${metaNotLinkedHtml()}</td>
      <td class="camp-num">${metaNotLinkedHtml()}</td>
      <td class="camp-num">${metaNotLinkedHtml()}</td>
    </tr>` : "";
  const notes = [
    data.meta_reported_leads_disclosure,
    `SQLs, won deals and won revenue: not linked yet — ${META_ADSET_NOT_LINKED_TEXT}.`,
    data.owner_declared_retired_disclosure || null,
  ].filter(Boolean);
  return `
    <div class="camp-scroll">
      <table class="camp-table meta-adset-table">
        <thead><tr>${head}</tr></thead>
        <tbody>${body}${totalRow}</tbody>
      </table>
    </div>
    ${notes.map((n) => `<p class="camp-keyword-note">${escapeHtml(n)}</p>`).join("")}`;
}

function renderMetaAdsetPanel(data) {
  return `
    <section class="dash-panel dash-anim meta-adset-panel" aria-label="Meta Ads — ad sets">
      <div class="dash-panel__header">
        <div>
          <h3 class="dash-panel__title">Meta Ads — ad sets</h3>
          <p class="dash-panel__sub">Each ad set is its own row with its own totals · never summed with Google Ads · settled days in each account's own time zone</p>
        </div>
      </div>
      ${renderMetaAdsetTable(data)}
      ${metaAdsetDayBasisHtml(data)}
    </section>`;
}

// ── PR-ADS-122: Campaign spend reconciliation drilldown ("View spend proof") ──
// A compact modal that proves a ROAS campaign row's spend: local canonical DB
// total vs a fresh Google Ads campaign-level API total, with a daily breakdown
// and an ad-group status breakdown. The UI never hides a mismatch.

let _spendProofOpen = false;
let _spendProofReqId = 0;

// Only a row with a real, immutable Google Ads campaign_id can be proven. The
// normalized fallback key ("unknown" / a slug) is not a queryable campaign_id.
function spendProofButton(campaignId, campaignName) {
  const id = campaignId == null ? "" : String(campaignId).trim();
  if (!id || id === "unknown") return "";
  if (!/^\d+$/.test(id)) return "";  // real Google Ads campaign ids are numeric
  return `<button type="button" class="spend-proof-link"
    data-spend-proof-campaign="${escapeHtml(id)}"
    data-spend-proof-name="${escapeHtml(campaignName || "")}">View spend proof</button>`;
}

function spendProofStatusBadge(status) {
  const map = {
    match: ["spend-proof-badge--ok", "Reconciled"],
    mismatch: ["spend-proof-badge--mismatch", "Mismatch"],
    unavailable: ["spend-proof-badge--unknown", "API total unavailable"],
  };
  const [cls, label] = map[status] || map.unavailable;
  return `<span class="spend-proof-badge ${cls}">${label}</span>`;
}

function spendProofVariance(value, cur) {
  if (value === null || value === undefined) return "—";
  const sign = value > 0 ? "+" : "";
  return sign + fmtCurrency(value, cur);
}

function renderSpendProofDaily(daily, cur) {
  if (!daily || !daily.length) {
    return '<p class="spend-proof-empty">No daily spend rows for this campaign in the window.</p>';
  }
  const body = daily.map((d) => `
    <tr class="${d.variance && Math.abs(d.variance) > 0.005 ? "spend-proof-row--diff" : ""}">
      <td>${escapeHtml(d.date || "—")}</td>
      <td>${fmtCurrency(d.local_cost, cur)}</td>
      <td>${d.api_cost === null || d.api_cost === undefined ? "—" : fmtCurrency(d.api_cost, cur)}</td>
      <td>${spendProofVariance(d.variance, cur)}</td>
      <td><span class="spend-proof-cov spend-proof-cov--${escapeHtml(d.coverage_state || "unverified")}">${escapeHtml(d.coverage_state || "unverified")}</span></td>
    </tr>`).join("");
  return `
    <table class="spend-proof-daily">
      <thead><tr><th>Date</th><th>Local cost</th><th>API cost</th><th>Variance</th><th>Coverage</th></tr></thead>
      <tbody>${body}</tbody>
    </table>`;
}

function renderSpendProofAdGroups(ag, cur) {
  if (!ag || !ag.available) {
    return '<p class="spend-proof-empty">Ad-group-level spend was not available — a paused/removed ad-group filter could not be confirmed or ruled out.</p>';
  }
  const rows = (ag.statuses || []).map((s) => `
    <tr>
      <td>${escapeHtml(s.status || "—")}</td>
      <td>${fmtCurrency(s.spend, cur)}</td>
      <td>${fmtCount(s.ad_group_count)}</td>
    </tr>`).join("");
  return `
    <table class="spend-proof-adgroups">
      <thead><tr><th>Ad group status</th><th>Spend</th><th>Ad groups</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="spend-proof-adgroup-totals">
      <span>Enabled only: <strong>${fmtCurrency(ag.enabled_only, cur)}</strong></span>
      <span>Paused/removed: <strong>${fmtCurrency(ag.paused_removed, cur)}</strong></span>
    </div>`;
}

function renderSpendProof(data) {
  const cur = data.currency_code || "GBP";
  const aliases = (data.mapped_aliases && data.mapped_aliases.length)
    ? escapeHtml(data.mapped_aliases.join("; ")) : "—";
  const causes = (data.possible_causes || []).map((c) => `<li>${escapeHtml(c)}</li>`).join("");
  const money = (v) => (v === null || v === undefined ? "Unavailable" : fmtCurrency(v, cur));
  const rows = [
    ["Campaign ID", escapeHtml(data.campaign_id || "—")],
    ["Canonical campaign name", escapeHtml(data.campaign_name || "—")],
    ["Mapped aliases", aliases],
    ["Window", `${escapeHtml(data.date_from || "—")} → ${escapeHtml(data.date_to || "—")}`],
    ["Account timezone", escapeHtml(data.account_time_zone || "—")],
    ["Native currency", escapeHtml(cur)],
    ["Coverage status", escapeHtml(data.coverage_status || "—")],
    ["Rows counted", fmtCount(data.rows_counted)],
    ["Date chunks verified", fmtCount(data.date_chunks_verified)],
  ];
  // PR-ADS-122: the totals are kept strictly separate so the campaign-level
  // reconciliation is never conflated with the Google Ads UI enabled-only view.
  const primaryRows = [
    ["Local canonical campaign total", money(data.local_canonical_campaign_total)],
    ["Fresh campaign API total (all statuses)", money(data.fresh_campaign_api_total_all_statuses)],
    ["Variance — local vs campaign API", `${spendProofVariance(data.variance_local_vs_fresh_campaign_api, cur)}${data.variance_local_vs_fresh_campaign_api_pct === null || data.variance_local_vs_fresh_campaign_api_pct === undefined ? "" : ` (${data.variance_local_vs_fresh_campaign_api_pct}%)`}`],
  ];
  const uiRows = [
    ["Ad-group total (all statuses)", money(data.fresh_ad_group_total_all_statuses)],
    ["Ad-group enabled-only", money(data.fresh_ad_group_total_enabled_only)],
    ["Ad-group paused/removed", money(data.fresh_ad_group_total_paused_removed)],
    ["Google Ads UI filter estimate", money(data.google_ui_filter_estimate)],
    ["Variance — UI estimate vs enabled-only", spendProofVariance(data.variance_google_ui_filter_estimate_vs_enabled_only, cur)],
    ["Variance — local vs ad-group all statuses", spendProofVariance(data.variance_local_vs_ad_group_all_statuses, cur)],
  ];
  const dupCount = (data.duplicate_local_rows || []).length;
  const dupNote = dupCount
    ? `<div class="spend-proof-warn">⚠ ${dupCount} duplicate local row date(s) detected for this campaign.</div>`
    : "";
  return `
    <div class="spend-proof-overlay">
      <div class="spend-proof-modal" role="dialog" aria-modal="true" aria-label="Campaign spend proof">
        <header class="spend-proof-header">
          <div>
            <div class="spend-proof-eyebrow">Spend reconciliation</div>
            <h3>${escapeHtml(data.campaign_name || "Campaign")} ${spendProofStatusBadge(data.status)}</h3>
          </div>
          <button type="button" class="spend-proof-close" data-spend-proof-x aria-label="Close">×</button>
        </header>
        <div class="spend-proof-body">
          ${dupNote}
          <dl class="spend-proof-grid">
            ${rows.map(([k, v]) => `<div><dt>${escapeHtml(k)}</dt><dd>${v}</dd></div>`).join("")}
          </dl>
          <h4 class="spend-proof-subhead">Primary reconciliation — local canonical vs fresh campaign API</h4>
          <p class="spend-proof-note">These are expected to match. A mismatch points to stale local spend, a date-boundary issue, or a campaign identity/mapping issue — not the ad-group filter.</p>
          <dl class="spend-proof-grid">
            ${primaryRows.map(([k, v]) => `<div><dt>${escapeHtml(k)}</dt><dd>${v}</dd></div>`).join("")}
          </dl>
          <h4 class="spend-proof-subhead">Google Ads UI estimate — enabled-only ad-group view</h4>
          <p class="spend-proof-note">Only the enabled-only ad-group total explains the Google Ads UI screenshot (Ad group status: Enabled). It is not assumed equal to the campaign-level API total.</p>
          <dl class="spend-proof-grid">
            ${uiRows.map(([k, v]) => `<div><dt>${escapeHtml(k)}</dt><dd>${v}</dd></div>`).join("")}
          </dl>
          <h4 class="spend-proof-subhead">Ad-group status breakdown</h4>
          ${renderSpendProofAdGroups(data.ad_group_breakdown, cur)}
          <h4 class="spend-proof-subhead">Daily breakdown</h4>
          ${renderSpendProofDaily(data.daily, cur)}
          <h4 class="spend-proof-subhead">Possible causes</h4>
          <ul class="spend-proof-causes">${causes || "<li>—</li>"}</ul>
        </div>
      </div>
    </div>`;
}

function spendProofContainer() {
  let el = document.getElementById("spend-proof-root");
  if (!el) {
    el = document.createElement("div");
    el.id = "spend-proof-root";
    document.body.appendChild(el);
  }
  return el;
}

function closeSpendProof() {
  _spendProofOpen = false;
  const el = document.getElementById("spend-proof-root");
  if (el) el.innerHTML = "";
}

async function openSpendProof(campaignId, campaignName) {
  const id = (campaignId || "").trim();
  if (!id) return;
  _spendProofOpen = true;
  const reqId = ++_spendProofReqId;
  const window_ = getRoasBusinessWindow();
  const root = spendProofContainer();
  root.innerHTML = `
    <div class="spend-proof-overlay">
      <div class="spend-proof-modal" role="dialog" aria-modal="true">
        <header class="spend-proof-header">
          <div><div class="spend-proof-eyebrow">Spend reconciliation</div>
          <h3>${escapeHtml(campaignName || "Campaign")}</h3></div>
          <button type="button" class="spend-proof-close" data-spend-proof-x aria-label="Close">×</button>
        </header>
        <div class="spend-proof-body"><p class="spend-proof-empty">Loading spend proof…</p></div>
      </div>
    </div>`;
  try {
    const res = await fetch(
      `/api/google-ads-spend-reconcile/campaign?window=${encodeURIComponent(window_)}&campaign_id=${encodeURIComponent(id)}`,
      { credentials: "same-origin" });
    if (reqId !== _spendProofReqId || !_spendProofOpen) return;
    if (!res.ok) {
      root.innerHTML = `
        <div class="spend-proof-overlay">
          <div class="spend-proof-modal" role="dialog" aria-modal="true">
            <header class="spend-proof-header"><h3>Spend proof unavailable</h3>
            <button type="button" class="spend-proof-close" data-spend-proof-x aria-label="Close">×</button></header>
            <div class="spend-proof-body"><p class="spend-proof-empty">Could not load reconciliation for this campaign.</p></div>
          </div>
        </div>`;
      return;
    }
    const data = await res.json();
    if (reqId !== _spendProofReqId || !_spendProofOpen) return;
    root.innerHTML = renderSpendProof(data);
  } catch (err) {
    console.error("[openSpendProof]", err);
    if (reqId !== _spendProofReqId || !_spendProofOpen) return;
    root.innerHTML = `
      <div class="spend-proof-overlay">
        <div class="spend-proof-modal" role="dialog" aria-modal="true">
          <header class="spend-proof-header"><h3>Spend proof error</h3>
          <button type="button" class="spend-proof-close" data-spend-proof-x aria-label="Close">×</button></header>
          <div class="spend-proof-body"><p class="spend-proof-empty">Error loading spend reconciliation.</p></div>
        </div>
      </div>`;
  }
}

document.addEventListener("click", (e) => {
  const open = e.target.closest("[data-spend-proof-campaign]");
  if (open) {
    openSpendProof(open.dataset.spendProofCampaign, open.dataset.spendProofName);
    return;
  }
  if (e.target.closest("[data-spend-proof-x]")) { closeSpendProof(); return; }
  // Backdrop click (only when the overlay itself is the target) closes the modal.
  if (e.target.classList && e.target.classList.contains("spend-proof-overlay")) {
    closeSpendProof();
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && _spendProofOpen) closeSpendProof();
});

// ── Countries — the one country page (PR-ADS-176B) ───────────────────────────
//
// The Countries evidence page (#/geo) and Country Revenue & Spend are one page
// (`roas-countries`), built in the Dashboard's visual language: its KPI cards,
// country table, Regional Mix, residual panel and drawer. It reads the mart's
// country view (`/api/revenue-performance?view=country`) on business windows,
// and decides nothing: the strict spend gate, every total (`country_totals`)
// and the regional mix (`regional_mix`) come from the server. A figure that is
// not published reads its withholding word with the reason — never "—", never 0.

let roasCountriesData = [];
let roasCountriesStatus = "idle";

// PR-ADS-112: the page's readiness needs the shared revenue truth layer to be
// safe AND country-level Google Ads geo spend to be available.
let roasCountrySourceHealth = null;

// Frontend-only decision-bucket filter (independent of the Campaign page).
let roasCountryFilter = "all";

// The market rows the table last rendered, in order: a row's drawer opens by
// its index here (data-country-row).
let roasCountryRenderedRows = [];

function filterRoasCountryRows(rows, filter) {
  if (!filter || filter === "all") return rows;
  return rows.filter((r) => r.verdict === filter);
}

// Business-value sort: outcomes (won deals, revenue) outrank verdict labels.
function sortRoasCountryRows(rows) {
  return [...rows].sort((a, b) =>
    (b.closed_won_deals || 0) - (a.closed_won_deals || 0) ||
    (b.won_revenue || 0) - (a.won_revenue || 0) ||
    (b.sqls || 0) - (a.sqls || 0) ||
    (b.spend || 0) - (a.spend || 0)
  );
}

// Unmapped spend stays visible rather than quietly disappearing.
function countryDisplayName(value) {
  const name = (value || "").trim();
  if (!name || name.toLowerCase() === "unknown") return "Unmapped";
  return name;
}

function isCountryRevenueDecisionReady(sourceHealth) {
  const sh = sourceHealth || {};
  return (
    isRevenueDecisionReady(sh) &&
    sh.country_spend_available === true &&
    sh.geo_country_mapping_status === "available"
  );
}

// A country spend figure. fmtMoney rounds to whole dollars, so 39 cents would
// print "$0" — a zero that was not measured. Above zero and below a dollar
// reads "<$1" (the rule the deleted Countries evidence page had, PR-ADS-167A).
function fmtCountrySpend(v) {
  if (v === null || v === undefined) return "Unavailable";
  if (v > 0 && v < 1) return "<$1";
  return fmtMoney(v);
}

// PR-ADS-176B-F1 (m5): a country spend in cents, for the residual's parts and
// its itemised countries — small figures where whole dollars would print "$0"
// (or "$3" for $2.50). Below a cent reads "<$0.01"; never "$0" for spend.
function fmtCountrySpendCents(v) {
  if (v === null || v === undefined) return "Unavailable";
  if (v > 0 && v < 0.01) return "<$0.01";
  return "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// m4: a total withheld because ONE market's figure is unknown names that
// market beside the reason ("Pakistan: some spend in this window has no
// country"). `html` is the canonical rendering of `metrics[key]`.
function withBlockedCountry(html, s, key) {
  const m = (((s || {}).metrics) || {})[ROW_METRIC_KEYS[key] || key] || {};
  if (m.status === "published" || !m.blocked_by_country) return html;
  return html.replace('<small class="metric-reason">',
    `<small class="metric-reason">${escapeHtml(m.blocked_by_country)}: `);
}

// The Dashboard KPI cards, over the server's `country_totals`: the listed
// markets' figures summed by the mart (never here), each {value, status,
// reason}. Customers have no card: a row's customers are distinct companies
// and are never summed across countries (docs/48).
function renderRoasCountrySummary(data) {
  const s = (data && data.country_totals) || {};
  // m7: the blockers link names the window of THIS response.
  const opts = campBlockerOpts(data);
  const residual = (roasCountriesData || []).find((r) => r && r.is_residual) || null;
  const known = (key) => s[key] !== null && s[key] !== undefined;
  const markets = (s.markets === null || s.markets === undefined) ? null : s.markets;
  const cards = [
    { key: "spend", label: "Country Spend",
      value: withBlockedCountry(rowMetricHtml(s, "spend", fmtCountrySpend, opts), s, "spend"),
      sub: `USD · ${markets === null ? "listed markets" : `${fmtCount(markets)} listed ${markets === 1 ? "market" : "markets"} in this window`}`,
      source: "Google Ads country spend · listed markets only", ok: known("spend") },
    // PR-ADS-177A: gated on THIS response's cohort first; under a published
    // cohort the total may still be withheld by a row (the country gate).
    { key: "sqls", label: COHORT_SQLS_LABEL,
      value: cohortCountPublication(data && data.cohort).publish
        ? withBlockedCountry(rowMetricHtml(s, "sqls", fmtCount, opts), s, "sqls")
          + ((s.sqls !== null && s.sqls !== undefined) ? cohortAsOfNote(data && data.cohort) : "")
        : cohortCountHtml(s.sqls, data && data.cohort, null),
      sub: `${countryLeadScopeLabel(s.lead_scope)} · listed markets${cohortThroughSuffix(data && data.cohort)}`,
      source: "HubSpot acquisition cohort", ok: known("sqls") },
    { key: "deals", label: "Closed-Won Deals",
      value: withBlockedCountry(rowMetricHtml(s, "closed_won_deals", fmtCount, opts), s, "closed_won_deals"),
      sub: "Google Ads-source · listed markets", source: "HubSpot Closed-Won", ok: known("closed_won_deals") },
    { key: "revenue", label: "Won Revenue",
      value: withBlockedCountry(rowMetricHtml(s, "won_revenue", fmtMoney, opts), s, "won_revenue"),
      sub: "USD · Google Ads-source · listed markets", source: "HubSpot Closed-Won", ok: known("won_revenue") },
    { key: "period_revenue_per_ad_spend", label: PERIOD_RATIO_LABEL,
      value: withBlockedCountry(rowMetricHtml(s, "period_revenue_per_ad_spend", fmtPeriodRatio, opts),
        s, "period_revenue_per_ad_spend"),
      sub: PERIOD_RATIO_DISCLOSURE, source: "Won revenue ÷ country spend, this window",
      ok: known("period_revenue_per_ad_spend") },
    { key: "residual", label: "Residual Spend",
      value: residual ? countryResidualSpendValue(residual) : escapeHtml("None in this window"),
      sub: "No listed market owns it · never distributed", source: "Outside the listed markets, or no country",
      ok: !residual || (residual.spend !== null && residual.spend !== undefined) },
  ];
  return `
    <div class="dash-kpi-grid ctry-kpi-grid">
      ${cards.map((c) => `
        <div class="dash-kpi-card dash-anim ${c.ok ? "" : "dash-kpi-card--unavailable"}" data-kpi="${c.key}">
          <div class="dash-kpi-card__label">${escapeHtml(c.label)}</div>
          <div class="dash-kpi-card__value">${c.value}</div>
          <div class="dash-kpi-card__sub">${escapeHtml(c.sub)}</div>
          <div class="dash-kpi-card__source">${escapeHtml(c.source)}</div>
        </div>`).join("")}
    </div>`;
}

// The residual row's spend, or its withholding with the reason.
function countryResidualSpendValue(r) {
  if (r.spend === null || r.spend === undefined) {
    return countrySpendUnavailableHtml(r.spend_reason || r.withheld_reason, "dash-unavailable");
  }
  return escapeHtml(fmtCountrySpendCents(r.spend));
}

// m8: the lead population the Leads and SQLs columns count (the mart declares
// it as `lead_scope`; PR-ADS-177A: the acquisition cohort, no longer
// db.revenue_repository.fetch_lead_quality).
const COUNTRY_LEAD_SCOPE_LABELS = {
  // PR-ADS-177A: the acquisition cohort, placed by the contact's own country.
  google_ads_cohort_contacts_by_contact_country: "Acquisition cohort · Google Ads contacts created in this window, by the contact's own HubSpot country · SQL = proven by lifecycle evidence as of the contact-funnel watermark",
};
function countryLeadScopeLabel(scope) {
  return COUNTRY_LEAD_SCOPE_LABELS[scope] || "Leads · by lead country";
}

function renderRoasCountryFilters() {
  const filters = [
    ["all", "All"],
    ["winner", "Winners"],
    ["watch", "Watch"],
    ["waste", "Waste"],
    ["learning", "Learning"],
  ];
  return `
    <div class="revenue-filter-chips" id="roas-country-filter-chips">
      ${filters.map(([key, label]) => `
        <button
          type="button"
          class="revenue-filter-chip ${roasCountryFilter === key ? "is-active" : ""}"
          data-roas-country-filter="${key}"
        >${label}</button>
      `).join("")}
    </div>
  `;
}

function renderCountryRevenueBlockedState(sourceHealth) {
  const sh = sourceHealth || {};
  const countrySpendReady = sh.country_spend_available === true && sh.geo_country_mapping_status === "available";
  // PR-ADS-177A: leads / SQLs block nothing (see renderRevenueBlockedState).
  const leadsReady = cohortLeadsReady(sh);
  const revenueReady = isCanonicalRevenueAvailable(sh);
  const chip = (label, ok, okText, warnText) =>
    `<div class="revenue-status-chip revenue-status-chip--${ok ? "ok" : "warning"}">
       <span>${label}</span><strong>${ok ? okText : warnText}</strong>
     </div>`;
  const nextStep = (!countrySpendReady
        ? "<strong>Next step:</strong> Run the Google Ads geo sync, then re-check this page."
        : "<strong>Next step:</strong> The canonical closed-won population is unavailable for this window — check the deal-ledger sync coverage in <strong>System Status</strong>.")
    + (leadsReady ? "" : COHORT_LEADS_WITHHELD_STEP);
  return `
    <div class="revenue-blocked-card">
      <div class="revenue-blocked-card__eyebrow">Revenue attribution</div>
      <h3>Country revenue &amp; spend is not ready yet</h3>
      <p>This page is blocked because country-level data is not safe enough for business decisions.</p>
      <div class="revenue-status-grid">
        ${chip("Country spend", countrySpendReady, "Available", "Rebuilding")}
        ${chip("Leads / SQLs", leadsReady, "Available", "Withheld")}
        ${chip("Revenue", revenueReady, "Connected", "Not connected")}
      </div>
      <div class="revenue-next-step">
        ${nextStep}
      </div>
      ${roasCountryCtaButtons()}
    </div>`;
}

function renderRoasCountrySafeEmptyState() {
  return `
    <div class="revenue-blocked-card revenue-empty-card">
      <div class="revenue-blocked-card__eyebrow">Revenue attribution</div>
      <h3>No country revenue found for this window</h3>
      <p>Google Ads country spend and HubSpot are ready, but no listed market and no residual has spend, leads or closed-won deals in this business window.</p>
    </div>`;
}

// PR-ADS-124: a real recovery CTA from the blocked country card. "Open Revenue
// Health" navigates; the Run actions are admin-only and kick the corresponding
// background job after navigating to Revenue Health. A non-admin sees guidance
// to ask an admin — never a dead-end "run geo sync" with no button. Defined here
// (function declarations hoist) so it never widens the renderRoasCountriesPage →
// table region that other tests scan.
// PR-ADS-176A truth audit MINOR 1: `run` ("all" default | "geo" | "spend" |
// "none") — no run button, and no "ask an admin to run", when nothing should
// be run.
// PR-ADS-176A truth audit MINOR 6: a withheld country spend reads
// "Unavailable" with its reason (GEO_ROW_SPEND_REASONS, the Countries page's
// own wording) — never "—", which reads as nothing to show.
function countrySpendUnavailableHtml(reason, cls) {
  const why = reason ? (countryReasonLabel(reason) || reason) : null;
  return `<span class="${cls}"${why ? ` title="${escapeHtml(why)}"` : ""}>Unavailable${why ? ` — ${escapeHtml(why)}` : ""}</span>`;
}

function roasCountrySpendCell(r) {
  // PR-ADS-176B: a row the gate withholds names the gate's gap.
  // PR-ADS-176B-F1 (M2): a $0 that stands only on the owner's labels says so.
  return (r.spend === null || r.spend === undefined)
    ? countrySpendUnavailableHtml(r.spend_reason || r.withheld_reason, "dash-unavailable")
    : escapeHtml(fmtCountrySpend(r.spend)) + (r.spend === 0 ? ownerZeroNote(r) : "");
}

function roasCountryCtaButtons(run) {
  const openHealth = `<button type="button" class="btn btn--secondary" data-country-cta="open-health">Open Revenue Health</button>`;
  const mode = run || "all";
  const isAdmin = _currentUser && _currentUser.role === "admin";
  const geoBtn = `<button type="button" class="btn btn--primary" data-country-cta="run-geo-sync">Run Google Ads Geo Sync</button>`;
  const spendBtn = `<button type="button" class="btn btn--secondary" data-country-cta="run-spend-backfill">Run Google Ads Spend Backfill</button>`;
  const ask = mode === "spend"
    ? `<p class="revenue-footnote">Ask an admin to run Google Ads Spend Backfill from Revenue Health.</p>`
    : `<p class="revenue-footnote">Ask an admin to run Google Ads Geo Sync from Revenue Health.</p>`;
  const runButtons = mode === "none" ? "" : !isAdmin ? ask
    : mode === "geo" ? geoBtn : mode === "spend" ? spendBtn : `${geoBtn}
       ${spendBtn}`;
  return `
    <div class="revenue-cta-actions">
      ${openHealth}
      ${runButtons}
    </div>`;
}

async function loadRoasCountries() {
  const window_ = getRoasBusinessWindow();
  const token = ++_revReqSeq.countries;
  setWindowRangeLoading("roas-countries-range");
  resetCountryDrilldowns();

  roasCountriesStatus = "loading";
  const body = document.getElementById("roas-countries-table-body");
  if (body) body.innerHTML = '<p class="empty-state" style="padding:var(--space-5)">Loading revenue attribution by country…</p>';

  try {
    // PR-ADS-126: PRIMARY truth is the canonical mart country view.
    const data = await fetchRevenuePerformance("country", window_);
    if (token !== _revReqSeq.countries) return;
    if (!data) {
      roasCountryMart = null;
      roasCountriesStatus = "error";
      if (body) body.innerHTML = renderMartUnavailable();
      return;
    }
    if (!_revResponseIsCurrent("countries", token, data)) return;
    roasCountryMart = data;
    roasCountryWindow = data.window || null;
    roasCountrySourceHealth = martCountrySourceHealth(data);
    roasCountriesData = data.rows || [];
    roasCountriesStatus = roasCountriesData.length ? "ok" : "empty";
    renderWindowRange("roas-countries-range", roasCountryWindow);
    // PR-ADS-130: on a geo MISMATCH, fetch the geo reconciliation root-cause so the
    // blocked card can name the likely cause + largest gap (never another blind "run
    // geo sync"). Only "mismatch" consumes roasCountryGeoReconcile — "unavailable"
    // does not use it and "reconciled_with_residual" renders the table — so we skip
    // the request for those states. Best-effort; the card renders either way.
    roasCountryGeoReconcile = null;
    const st = data.spend_truth || {};
    if (st.country_spend_status === "mismatch") {
      try {
        const gr = await fetch(`/api/google-ads-geo-reconcile?window=${encodeURIComponent(window_)}`, { credentials: "same-origin" });
        if (gr.ok && token === _revReqSeq.countries) roasCountryGeoReconcile = await gr.json();
      } catch (e) { /* best-effort root-cause */ }
      if (token !== _revReqSeq.countries) return;
    }
    renderRoasCountriesPage();
  } catch (err) {
    console.error("[loadRoasCountries]", err);
    if (token !== _revReqSeq.countries) return;
    roasCountryMart = null;
    roasCountriesStatus = "error";
    if (body) body.innerHTML = renderMartUnavailable();
  }
}

// Country ROAS requires a complete canonical denominator AND geo reconciliation.
function countryRoasUnavailable(sh) {
  const h = sh || {};
  if (h.country_roas_available === true) return false;
  if (h.country_roas_available === false) return true;
  // Backward-compatible fallback when the explicit flag is absent.
  return h.country_spend_reconciled === false || h.spend_coverage_status !== "complete";
}

function renderRoasCountriesPage() {
  const tableBody = document.getElementById("roas-countries-table-body");
  if (!tableBody) return;
  roasCountryRenderedRows = [];

  const data = roasCountryMart;
  if (!data) { tableBody.innerHTML = renderMartUnavailable(); return; }
  // PR-ADS-167A: no completed day in the window yet — nothing was measured,
  // so no card and no table: the disclosure only.
  if (windowHasNoSettledDay(data.window)) {
    tableBody.innerHTML = renderNoSettledDayCard(data.window);
    return;
  }

  // Blocked: the MART says revenue truth is not decision-ready (lead grain /
  // revenue integration). The frontend does not decide this.
  if (!data.readiness || data.readiness.revenue_decision_ready !== true) {
    tableBody.innerHTML = renderCountryRevenueBlockedState(roasCountrySourceHealth);
    return;
  }

  // The strict spend gate (owner decision 2, PR-ADS-176B) is the MART's verdict,
  // spend_truth.country_spend_status. Only "verified" and (PR-ADS-131)
  // "reconciled_with_residual" render figures; every other status — mismatch,
  // any gap, FX only — shows the gap-driven card (PR-ADS-176A) and no number,
  // never the number with a warning.
  const st = data.spend_truth || {};
  const residualMode = st.country_spend_status === "reconciled_with_residual";
  if (st.country_spend_status !== "verified" && !residualMode) {
    tableBody.innerHTML = renderCountrySpendUnreconciledState(st);
    return;
  }

  // The residual bucket is pinned regardless of the decision filter (it is not a
  // real country and must stay visible); real country rows are sorted + filtered.
  const residualRows = roasCountriesData.filter((r) => r && r.is_residual);
  const realRows = roasCountriesData.filter((r) => !(r && r.is_residual));

  // Safe but no closed-won revenue attributed to countries (and no residual row).
  if (!realRows.length && !residualRows.length) {
    tableBody.innerHTML = renderRoasCountrySafeEmptyState();
    return;
  }

  const sorted = sortRoasCountryRows(realRows);
  const filtered = filterRoasCountryRows(sorted, roasCountryFilter).concat(residualRows);

  // PR-ADS-176B: the Dashboard's Countries layout — KPI cards, the country
  // table, Regional Mix beside the residual, and the truth footer.
  tableBody.innerHTML = `
    ${ownerLabelMismatchNotice(st.owner_labelled_geo)}
    ${residualMode ? renderCountryResidualWarning(st) : ""}
    ${renderRoasCountrySummary(data)}
    ${martCohortFunnelHtml(data.cohort, { title: "Lifecycle funnel",
      sub: "Google Ads-sourced HubSpot contacts created in this window, every country — the residual included, so not only the listed markets above — and how many have reached each stage so far",
      stageNotes: { reached_sql: [countryFunnelResidualNote(data.cohort,
        (data.rows || []).find((r) => r && r.is_residual) || null)] } })}
    <section class="dash-panel dash-anim ctry-leaderboard-panel" aria-label="Countries">
      <div class="dash-panel__header">
        <div>
          <h3 class="dash-panel__title">Countries</h3>
          <p class="dash-panel__sub">Every listed market with activity, then the residual · click a market for its closed-won deals · ${escapeHtml(PERIOD_RATIO_LABEL)}: ${escapeHtml(PERIOD_RATIO_DISCLOSURE)}</p>
        </div>
      </div>
      ${renderRoasCountryFilters()}
      ${renderRoasCountryTable(filtered, campBlockerOpts(data))}
    </section>
    <div class="dash-main-grid ctry-lower-grid">
      <section class="dash-panel dash-anim ctry-region-panel" aria-label="Regional mix">
        ${renderCountriesRegionalMix(data)}
      </section>
      <aside class="dash-panel dash-anim ctry-residual-panel" aria-label="Residual">
        ${renderCountriesResidualPanel(residualRows[0] || null, campBlockerOpts(data))}
      </aside>
    </div>
    ${renderCountriesTruthFooter(data)}
  `;
}

// The Regional Mix panel (Dashboard → Countries' own), over the server's
// `regional_mix`: each region's figures summed by the mart over its listed
// markets, each {value, status, reason}. A region whose figure is not
// published draws no bar and says why — never a 0 and never "—".
function _countriesRegionalBarMetric(rows) {
  const anyPositive = (key) => rows.some((r) => (r[key] || 0) > 0);
  if (anyPositive("won_revenue")) return "won_revenue";
  if (anyPositive("closed_won_deals")) return "closed_won_deals";
  if (anyPositive("spend")) return "spend";
  return null;
}

function renderCountriesRegionalMix(data) {
  const rows = (data && data.regional_mix) || [];
  const header = `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Regional Mix</h3>
        <p class="dash-panel__sub">Where spend, closed-won deals and revenue concentrate · listed markets only</p>
      </div>
    </div>`;
  if (!rows.length) {
    return `${header}<p class="rev-breakdown-empty">No listed market has activity in this window.</p>`;
  }
  const opts = campBlockerOpts(data);
  const metricKey = _countriesRegionalBarMetric(rows);
  const fmt = { won_revenue: fmtMoney, closed_won_deals: fmtCount, spend: fmtCountrySpend }[metricKey];
  const metricLabel = { won_revenue: "won revenue", closed_won_deals: "closed-won deals", spend: "spend" }[metricKey] || "activity";
  const max = metricKey ? Math.max(1, ...rows.map((r) => (r[metricKey] === null || r[metricKey] === undefined) ? 0 : r[metricKey])) : 0;
  const bars = rows.map((r) => {
    const val = metricKey ? r[metricKey] : null;
    const known = val !== null && val !== undefined;
    const pct = (metricKey && known && max > 0) ? Math.max(2, (val / max) * 100) : 0;
    const barVal = !metricKey ? escapeHtml(`${fmtCount(r.countries || 0)} ${r.countries === 1 ? "market" : "markets"}`)
      : withBlockedCountry(rowMetricHtml(r, metricKey, fmt, opts), r, metricKey);
    return `
      <li class="ctry-region-row">
        <div class="ctry-region-row__top">
          <span class="ctry-region-row__name"><span class="ctry-dot ctry-dot--sql"></span>${escapeHtml(r.region || "Unknown / Needs Review")}</span>
          <span class="ctry-region-row__val">${barVal}</span>
        </div>
        <div class="ctry-region-bar"><span class="ctry-region-bar__fill" style="width:${pct.toFixed(1)}%;background:#129ef5"></span></div>
        <div class="ctry-region-row__meta">
          <span>${escapeHtml(fmtCount(r.countries || 0))} ${r.countries === 1 ? "market" : "markets"}</span>
          <span>Spend ${withBlockedCountry(rowMetricHtml(r, "spend", fmtCountrySpend, opts), r, "spend")}</span>
          <span>SQLs ${rowMetricHtml(r, "sqls", fmtCount, opts)}${(r.sqls !== null && r.sqls !== undefined) ? cohortAsOfNote(data && data.cohort) : ""}</span>
          <span>Won deals ${rowMetricHtml(r, "closed_won_deals", fmtCount, opts)}</span>
        </div>
      </li>`;
  }).join("");
  return `${header}
    <p class="ctry-region-note">Bars scaled by ${escapeHtml(metricLabel)}${metricKey ? "" : " — nothing to scale in this window"}.</p>
    <ul class="ctry-region-list">${bars}</ul>`;
}

// The residual panel: the one bucket no listed market owns. Its spend parts
// are named (outside the listed markets / no country / not assigned by
// Google's geographic view) — never spread across real countries.
function renderCountriesResidualPanel(res, opts) {
  const header = `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Unknown / Unattributed country</h3>
        <p class="dash-panel__sub">Isolated — never distributed across markets</p>
      </div>
    </div>`;
  if (!res) {
    return `${header}<p class="rev-breakdown-empty">No residual in this window.</p>`;
  }
  opts = opts || campBlockerOpts(roasCountryMart);
  const c = res.spend_components || null;
  // A part the server measured at zero adds nothing to the breakdown.
  const part = (label, v) => (v === null || v === undefined || v === 0) ? ""
    : `<li>${escapeHtml(label)}: ${escapeHtml(fmtCountrySpendCents(v))}</li>`;
  const parts = c ? `
    <ul class="ctry-residual-parts">
      ${part("Countries outside the listed markets", c.outside_listed_markets)}
      ${part("Spend with no country", c.country_unidentified)}
      ${part("Not assigned to a location by Google Ads", c.geo_unassigned)}
    </ul>` : "";
  const notes = (Array.isArray(res.attribution_notes) ? res.attribution_notes : [])
    .filter((n) => /never spread|not one of the listed markets/i.test(String(n)));
  return `${header}
    <dl class="ctry-residual-tiles">
      <div><dt>Residual spend</dt><dd>${countryResidualSpendValue(res)}</dd></div>
      <div><dt>Closed-won deals</dt><dd>${rowMetricHtml(res, "closed_won_deals", fmtCount, opts)}</dd></div>
      <div><dt>Won revenue</dt><dd>${rowMetricHtml(res, "won_revenue", fmtMoney, opts)}</dd></div>
      <div><dt>${escapeHtml(COHORT_SQLS_LABEL + cohortThroughSuffix(opts.cohort))}</dt><dd>${cohortCountHtml(res.sqls, opts.cohort, res)}</dd></div>
    </dl>
    ${parts}
    <p class="ctry-residual-note">${escapeHtml(notes[0] || "Spend, deals and leads no listed market owns are preserved separately — never spread across real countries.")}</p>`;
}

// The Dashboard truth footer, from the mart's own statuses.
function renderCountriesTruthFooter(data) {
  const st = (data && data.spend_truth) || {};
  const ok = (v) => v === "verified" || v === "complete" || v === "reconciled_with_residual";
  const chip = (label, status) => `
    <span class="dash-footer-chip" title="${escapeHtml(`${label}: ${labelizeStatus(status).replace(/_/g, " ")}`)}">
      <span class="dash-dot ${ok(status) ? "dash-dot--ok" : "dash-dot--warn"}"></span>${escapeHtml(label)} · ${escapeHtml(labelizeStatus(status).replace(/_/g, " "))}
    </span>`;
  return `
    <footer class="dash-truth-footer">
      ${chip("Campaign spend", st.campaign_spend_status)}
      ${chip("FX", st.fx_status)}
      ${chip("Geo ↔ campaign spend reconciliation", st.country_spend_status)}
      <span class="dash-footer-chip">Revenue: HubSpot closed-won · Google Ads-source</span>
    </footer>`;
}

// PR-ADS-131: compact warning shown above the country table when spend is
// reconciled WITH an unattributed residual. It explains that most spend was
// assigned to countries and the shortfall is shown as "Unknown / Unattributed country"
// instead of being spread across real countries. Never presents "run geo sync" as
// the fix — the residual is a by-design property of the geographic_view report.
function renderCountryResidualWarning(spendTruth) {
  const st = spendTruth || {};
  const cur = st.native_currency || "GBP";
  const resNative = (st.country_residual_native == null)
    ? "some spend" : fmtNativeMoney(st.country_residual_native, cur);
  const pct = (st.country_residual_pct == null) ? "" : ` (${st.country_residual_pct}%)`;
  return `
    <div class="revenue-residual-warning">
      <div class="revenue-residual-warning__eyebrow">Country spend includes an unattributed residual</div>
      <p>Google Ads assigned most spend to countries, but ${resNative}${pct} could not be assigned to a country by the geo report.</p>
      <p class="revenue-residual-warning__note">The residual is shown as “Unknown / Unattributed country” instead of being spread across real countries. Real country rows use country-attributed spend only.</p>
    </div>`;
}

// PR-ADS-127: the country spend denominator can be untrusted for two distinct
// reasons, and the copy must match the cause (both still withhold ROAS — never a
// fabricated denominator):
//   - "mismatch"     → geo spend exists but does NOT reconcile with canonical
//                      campaign spend (point at the Geo Sync mismatch totals).
//   - "unavailable"  → there is NO canonical geo (country) spend for this window
//                      yet (run the Geo Sync to populate it). Saying it "does not
//                      reconcile" here would be wrong — there is nothing to
//                      reconcile.
// PR-ADS-128: cause-specific Country ROAS blocked card. Takes the mart's whole
// spend_truth so it can show exactly which spend gate failed (campaign spend, FX,
// or country geo) and a next step matched to the cause. Country geo can be
// untrusted for two distinct reasons — both withhold the trusted ROAS table, and
// neither fabricates a denominator:
//   - "mismatch"    → geo spend exists but does NOT reconcile with canonical
//                     campaign spend (review the Geo Sync mismatch totals).
//   - "unavailable" → there is no canonical geo spend yet (run the Geo Sync).
// PR-ADS-176A: the next step for an UNAVAILABLE country spend gate. Truth
// audit MINOR 1: it follows ONE code — spend_truth.country_gap_primary, the gap
// the server's country_gap_message describes (google_ads_geo_sync_service
// primary_geo_gap) — so the cause, the next step and the run buttons never
// name two different gaps. "Run …" is offered only where running fixes it;
// before the morning sync the newest day is simply not re-read yet.
const COUNTRY_GAP_NEXT_STEP = {
  spend_not_reread_since_day_ended: "Nothing to run: the morning sync re-reads the newest day of this window. Re-check this page after it.",
  window_includes_day_in_progress: "Nothing to run: choose a window that ends before today, or re-check after the morning sync.",
  no_settled_day_in_window: "Nothing to run: no day of this window has ended yet. Re-check after the first morning sync in it.",
  spend_reread_failed: "Re-run Google Ads Geo Sync in Revenue Health: the sync that should have re-read the newest day failed.",
  account_time_zone_unknown: "Open Revenue Health: the Google Ads account time zone is not on record.",
  geo_coverage_incomplete: "Run Google Ads Geo Sync in Revenue Health to populate country spend, then re-check this page.",
  geo_coverage_has_failed_chunks: "Re-run Google Ads Geo Sync in Revenue Health for the failed range, then re-check this page.",
  campaign_coverage_incomplete: "Run Google Ads Spend Backfill in Revenue Health for the missing range, then re-check this page.",
  campaign_spend_unreadable: "Open Revenue Health: canonical campaign spend could not be read.",
  geo_rows_unreadable: "Open Revenue Health: the synced country spend could not be read.",
  geo_coverage_ledger_unreadable: "Open Revenue Health: the record of synced days could not be read.",
  missing_geo_dates: "Re-run Google Ads Geo Sync in Revenue Health for the days with no geographic spend.",
  campaign_spend_without_geo: "Re-run Google Ads Geo Sync in Revenue Health for the campaigns with no geographic spend.",
  fx_coverage_incomplete: "Open Revenue Health and review FX coverage for this window.",
  comparison_not_like_for_like: "Open Revenue Health: the campaign and geographic totals span different accounts or currencies.",
  totals_differ: "Open Revenue Health and review the Google Ads Geo Sync totals.",
  // PR-ADS-176C-F2 (m4).
  google_ads_customer_id_not_configured: "Configure the Google Ads account (GOOGLE_ADS_CUSTOMER_ID); country spend is withheld until it is.",
};
// Which run buttons a gap warrants ("geo" / "spend" / none).
const COUNTRY_GAP_RUN = {
  geo_coverage_incomplete: "geo", geo_coverage_has_failed_chunks: "geo",
  spend_reread_failed: "geo", missing_geo_dates: "geo", campaign_spend_without_geo: "geo",
  campaign_coverage_incomplete: "spend",
  // Round 2: the Spend Backfill writes the account time zone.
  account_time_zone_unknown: "spend",
};
const COUNTRY_GAP_REVIEW_STEP = "Open Revenue Health and review the Google Ads Geo Sync coverage for this window.";
function countryGapNextStep(primary) {
  return primary ? (COUNTRY_GAP_NEXT_STEP[primary] || null) : null;
}
// Round 2: the runs ALL gap codes warrant ("none" adds nothing), so a
// secondary gap a run fixes still offers it; cause and step stay the primary's.
function countryGapRuns(codes) {
  const runs = new Set((codes || []).map((c) => COUNTRY_GAP_RUN[c]).filter(Boolean));
  if (runs.has("geo") && runs.has("spend")) return "all";
  return runs.has("geo") ? "geo" : runs.has("spend") ? "spend" : "none";
}
// { nextStep, run } for an unavailable card; run is "all" | "geo" | "spend" | "none".
function countryGapGuidance(st, isMismatch) {
  if (isMismatch) return { nextStep: "Open Revenue Health and review the Google Ads Geo Sync totals.", run: "all" };
  const primary = st.country_gap_primary || null;
  const codes = Array.isArray(st.country_gap_codes) ? st.country_gap_codes : [];
  if (primary) {
    return { nextStep: countryGapNextStep(primary) || COUNTRY_GAP_REVIEW_STEP,
      run: countryGapRuns([primary, ...codes]) };
  }
  // Round 2: a gap the payload names without its primary is reviewed, never
  // answered with the "no geo spend yet — run the Geo Sync" default (fail closed).
  if (codes.length || st.country_gap_message) return { nextStep: COUNTRY_GAP_REVIEW_STEP, run: "none" };
  return { nextStep: "Run Google Ads Geo Sync in Revenue Health to populate country spend, then re-check this page.", run: "all" };
}

function renderCountrySpendUnreconciledState(spendTruth) {
  const st = spendTruth || {};
  const status = st.country_spend_status;
  const isMismatch = status === "mismatch";
  // PR-ADS-176A: name WHICH gap (spend_truth.country_gap_message / _codes);
  // the "no geo spend yet" copy only when the payload names none.
  const gapMessage = (!isMismatch && st.country_gap_message) ? String(st.country_gap_message) : null;
  const cause = isMismatch
    ? "Country geo spend exists, but it does not reconcile with canonical Google Ads campaign spend for this window."
    : (gapMessage
      || "There is no canonical Google Ads geo spend for this window yet, so country Period Revenue / Ad Spend cannot be computed safely.");
  const guide = countryGapGuidance(st, isMismatch);
  const nextStep = guide.nextStep;
  const chip = (label, value, ok) =>
    `<div class="revenue-status-chip revenue-status-chip--${ok ? "ok" : "warning"}">
       <span>${label}</span><strong>${escapeHtml(value)}</strong>
     </div>`;
  // PR-ADS-129: show the exact totals + variance for a mismatch (native currency,
  // never fabricated). Only shown when the mart provides real geo numbers.
  const cur = st.native_currency || "GBP";
  const hasTotals = isMismatch && st.campaign_spend_total != null && st.country_geo_total != null;
  const pct = (st.country_spend_variance_pct == null) ? "" : ` (${st.country_spend_variance_pct}%)`;
  const tol = fmtTolerancePct(st.country_spend_tolerance);
  const totalsHtml = hasTotals
    ? `<div class="revenue-summary-strip revenue-summary-strip--four revenue-geo-variance">
         <div><span>Campaign spend</span><strong>${fmtNativeMoney(st.campaign_spend_total, cur)}</strong></div>
         <div><span>Geo spend</span><strong>${fmtNativeMoney(st.country_geo_total, cur)}</strong></div>
         <div><span>Variance</span><strong>${st.country_spend_variance == null ? "—" : fmtNativeMoney(st.country_spend_variance, cur)}${pct}</strong></div>
         <div><span>Tolerance</span><strong>${escapeHtml(tol || "—")}</strong></div>
       </div>`
    : "";
  // PR-ADS-130: root-cause (from the geo reconciliation audit) so the blocked card
  // names the LIKELY CAUSE + LARGEST GAP instead of another blind "run geo sync".
  const gr = (isMismatch && roasCountryGeoReconcile) ? roasCountryGeoReconcile : null;
  let rootCauseHtml = "";
  let effectiveNextStep = nextStep;
  if (gr) {
    const cgap = (gr.largest_campaign_gaps && gr.largest_campaign_gaps[0]) || null;
    const dgap = (gr.largest_daily_gaps && gr.largest_daily_gaps[0]) || null;
    const largest = cgap
      ? `${escapeHtml(cgap.campaign_name || cgap.campaign_id || "campaign")}: ${fmtNativeMoney(cgap.variance_native, cur)}`
      : (dgap ? `${escapeHtml(dgap.date || "")}: ${fmtNativeMoney(dgap.variance_native, cur)}` : null);
    effectiveNextStep = geoReconcileNextActionText(gr.next_action);
    rootCauseHtml = `
      <div class="revenue-status-label">Likely cause</div>
      <p class="revenue-geo-cause">${escapeHtml(geoReconcileCauseText(gr.reason))}</p>
      ${largest ? `<div class="revenue-status-label">Largest gap</div><p class="revenue-geo-next">${largest}</p>` : ""}`;
  }
  return `
    <div class="revenue-blocked-card">
      <div class="revenue-blocked-card__eyebrow">Google Ads spend truth</div>
      <h3>Country Period Revenue / Ad Spend is blocked</h3>
      <p>${escapeHtml(cause)}</p>
      ${totalsHtml}
      <div class="revenue-status-label">Current status</div>
      <div class="revenue-status-grid">
        ${chip("Campaign spend", labelizeStatus(st.campaign_spend_status), st.campaign_spend_status === "verified")}
        ${chip("FX coverage", labelizeStatus(st.fx_status), st.fx_status === "verified")}
        ${chip("Country geo spend", labelizeStatus(status), false)}
      </div>
      ${rootCauseHtml}
      <div class="revenue-next-step"><strong>Next step:</strong> ${escapeHtml(effectiveNextStep)}</div>
      ${roasCountryCtaButtons(guide.run)}
    </div>`;
}

// PR-ADS-176B: an outside-market country itemised under the residual
// (`outside_listed_market_items`, PR-ADS-176A). Its spend is a breakdown of the
// residual's — never a market, never in any total. Its outcomes are not split
// per country: they read "In residual", never 0.
function countryInResidualCell() {
  return '<span class="ctry-in-residual" title="Counted in the residual row above, not per country">In residual</span>';
}

function renderCountryResidualItemRow(item) {
  return `
    <tr class="ctry-row ctry-row--residual ctry-row--residual-item" aria-label="${escapeHtml(item.country || "Country")} — in the residual, not a listed market">
      <td class="ctry-td ctry-td--name"><span class="ctry-residual-item__arrow" aria-hidden="true">↳</span>${escapeHtml(item.country || item.country_code || "Country")}${ownerLabelledCountryNote(item.provenance)} <span class="revenue-residual-badge">In residual</span></td>
      <td class="ctry-td--region">Outside the listed markets</td>
      <td class="ctry-num">${(item.spend === null || item.spend === undefined) ? countrySpendUnavailableHtml(null, "dash-unavailable") : escapeHtml(fmtCountrySpendCents(item.spend))}</td>
      <td class="ctry-num">${countryInResidualCell()}</td>
      <td class="ctry-num">${countryInResidualCell()}</td>
      <td class="ctry-num">${countryInResidualCell()}</td>
      <td class="ctry-num">${countryInResidualCell()}</td>
      <td class="ctry-num">${countryInResidualCell()}</td>
      <td>${countryInResidualCell()}</td>
      <td><span class="ctry-status ctry-status--residual">Part of the residual</span></td>
    </tr>`;
}

function renderRoasCountryTable(rows, opts) {
  if (!rows.length) {
    return `
      <div class="revenue-empty-inline">
        No countries match this decision filter.
      </div>
    `;
  }
  // m7: the blockers link names the window of the response on screen.
  opts = opts || campBlockerOpts(roasCountryMart);
  // PR-ADS-177A: the cohort of THIS response, passed in by the caller.
  const cohort = opts.cohort;
  const leadsTitle = escapeHtml(countryLeadScopeLabel((rows.find((r) => r && r.lead_scope) || {}).lead_scope));
  const headerRow = `
    <tr>
      <th>Country</th>
      <th>Region</th>
      <th>Spend</th>
      <th title="${leadsTitle}">Leads<span class="th-sub"> · contacts acquired in window${escapeHtml(cohortThroughSuffix(cohort))}</span></th>
      <th title="${leadsTitle}">SQLs<span class="th-sub"> · contacts acquired in window${escapeHtml(cohortThroughSuffix(cohort))}</span></th>
      <th>Won Deals</th>
      <th title="Distinct proven HubSpot companies — not additive across countries">Customers</th>
      <th>Won Revenue</th>
      <th title="${escapeHtml(PERIOD_RATIO_DISCLOSURE)}">${PERIOD_RATIO_LABEL}</th>
      <th>Decision</th>
    </tr>
  `;

  // PR-ADS-131 / 176B: the residual row is NOT a real country — muted, a
  // "Residual" badge, no region, an "Unattributed" decision and no drawer. Its
  // figures are its own (deals and leads whose country is no listed market);
  // its period ratio is unavailable (truth audit M2). Its outside-market
  // countries follow it as "In residual" sub-rows (PR-ADS-176A items).
  // A real market row opens its drawer (the Dashboard's row pattern).
  const market = [];
  const bodyRows = rows.map((r) => {
    if (r && r.is_residual) {
      const items = Array.isArray(r.outside_listed_market_items) ? r.outside_listed_market_items : [];
      return `
    <tr class="ctry-row ctry-row--residual revenue-residual-row" aria-label="Unattributed residual (not a country)">
      <td class="ctry-td ctry-td--name"><span class="ctry-dot ctry-dot--muted"></span><span class="revenue-residual-name">Unknown / Unattributed country</span> <span class="revenue-residual-badge">Residual</span></td>
      <td class="ctry-td--region">Not a market</td>
      <td class="ctry-num">${(r.spend === null || r.spend === undefined) ? roasCountrySpendCell(r) : residualSpendCell(r)}</td>
      <td class="ctry-num">${cohortCountHtml(r.leads, cohort, r)}</td>
      <td class="ctry-num">${cohortCountHtml(r.sqls, cohort, r)}</td>
      <td class="ctry-num">${rowMetricHtml(r, "closed_won_deals", fmtCount, opts)}</td>
      <td class="ctry-num">${rowMetricHtml(r, "customers", fmtCount, opts)}</td>
      <td class="ctry-num">${rowMetricHtml(r, "won_revenue", fmtMoney, opts)}</td>
      <td>${rowMetricHtml(r, "period_revenue_per_ad_spend", fmtPeriodRatio, opts)}</td>
      <td><span class="decision-badge decision-badge--unattributed">Unattributed</span></td>
    </tr>${items.map(renderCountryResidualItemRow).join("")}`;
    }
    const i = market.push(r) - 1;
    const name = countryDisplayName(r.country);
    return `
    <tr class="ctry-row" data-country-row="${i}" tabindex="0" role="button" aria-label="${escapeHtml(`Open ${name || "country"} detail`)}">
      <td class="ctry-td ctry-td--name">${escapeHtml(name)}${spendOwnerNote(r)}</td>
      <td class="ctry-td--region">${escapeHtml(r.region || "Unknown / Needs Review")}</td>
      <td class="ctry-num">${roasCountrySpendCell(r)}</td>
      <td class="ctry-num">${cohortCountHtml(r.leads, cohort, r)}</td>
      <td class="ctry-num">${cohortCountHtml(r.sqls, cohort, r)}</td>
      <td class="ctry-num">${rowMetricHtml(r, "closed_won_deals", fmtCount, opts)}</td>
      <td class="ctry-num">${rowMetricHtml(r, "customers", fmtCount, opts)}</td>
      <td class="ctry-num">${rowMetricHtml(r, "won_revenue", fmtMoney, opts)}</td>
      <td>${rowMetricHtml(r, "period_revenue_per_ad_spend", fmtPeriodRatio, opts)}</td>
      <td>${getBusinessVerdictBadge(r.verdict)}</td>
    </tr>`;
  }).join("");
  roasCountryRenderedRows = market;

  return `
    <div class="ctry-scroll">
      <table class="ctry-table">
        <thead>${headerRow}</thead>
        <tbody>${bodyRows}</tbody>
      </table>
    </div>
  `;
}

// ── PR-ADS-132: Country Revenue & Spend row drilldown (clients / deals behind revenue) ──
// Expand control toggles a per-country drawer that lazily fetches the closed-won
// client/deal detail from the read-only country-detail endpoint. Details are
// rendered exactly as the backend provides them — missing company/contact/deal
// ids and amounts show "unavailable", never a fabricated value, and the deal is
// never hidden. Residual rows have no expand button (handled in the table render).
// Reuses drilldownValue() (defined for the campaign drawer) as the safe formatter.

// Lazy-load cache/state keyed per window+country so a drawer is fetched at most
// once and re-opening never refetches.
let roasCountryExpandedKey = null;       // eslint-disable-line no-unused-vars
let roasCountryDrilldownCache = {};
let roasCountryDrilldownLoading = {};    // eslint-disable-line no-unused-vars
let roasCountryDrilldownError = {};      // eslint-disable-line no-unused-vars
// m7: bumped by every loadRoasCountries; a deal list read under an older
// generation is not cached or shown.
let roasCountryDrilldownGen = 0;

// The business window of the country response on screen (m7), not the
// selector's: a drawer lists the deals of the rows it sits under.
function roasCountryWindowKey() {
  return ((roasCountryMart || {}).window || {}).key || getRoasBusinessWindow();
}

// PR-ADS-176B-F1 (m7), called by loadRoasCountries: a reload (Refresh, a new
// window, a settled-day rollover) discards every cached deal list, and a deal
// list still in flight from before it is dropped on arrival — never shown
// beside the new rows' counts.
function resetCountryDrilldowns() {
  roasCountryDrilldownCache = {};
  roasCountryDrilldownGen += 1;
}

// Stable key: window + country identity (prefer the safer ISO code).
function countryDrilldownKey(country, countryCode, windowKey) {
  const id = (countryCode || country || "").toString();
  return `${windowKey || roasCountryWindowKey()}:${id}`;
}

// One detail <tr> inside the drawer table. Every cell routes through
// drilldownValue() so a missing value renders "unavailable" — never undefined,
// null, or a fake $0.00 amount.
function renderCountryDrilldownRow(d) {
  const amount = d.deal_amount_usd == null
    ? drilldownValue(null)
    : fmtMoney(d.deal_amount_usd);
  const campaign = d.campaign_name || d.raw_campaign_name || null;
  const attribution = d.match_source || d.match_status || null;
  return `
    <tr>
      <td>${drilldownValue(d.company_name)}</td>
      <td>${drilldownValue(d.company_record_id)}</td>
      <td>${drilldownValue(d.main_contact_name)}</td>
      <td>${drilldownValue(d.main_contact_record_id)}</td>
      <td>${drilldownValue(d.deal_name)}</td>
      <td>${drilldownValue(d.deal_record_id)}</td>
      <td>${amount}</td>
      <td>${drilldownValue(d.deal_close_date)}</td>
      <td>${drilldownValue(campaign)}</td>
      <td>${drilldownValue(attribution)}</td>
    </tr>`;
}

function renderCountryDrilldown(data) {
  const status = (data && data.source_health && data.source_health.status) || "ready";
  if (status === "database_unavailable") {
    return `<p class="country-drilldown-muted">Country client details could not be loaded.</p>`;
  }
  const details = (data && data.details) || [];
  if (!details.length) {
    return `<div class="country-drilldown-empty">No attributed closed-won client detail found for this country in this window.</div>`;
  }
  const header = `
    <tr>
      <th>Company</th><th>Company ID</th><th>Main Contact</th><th>Contact ID</th>
      <th>Deal</th><th>Deal ID</th><th>Amount</th><th>Close Date</th>
      <th>Campaign / Source</th><th>Attribution</th>
    </tr>`;
  const body = details.map(renderCountryDrilldownRow).join("");
  return `
    <div class="country-drilldown-title">Clients / Deals Behind This Country</div>
    <div class="table-scroll country-drilldown-scroll">
      <table class="data-table revenue-decision-table country-drilldown-table">
        ${header}
        <tbody>${body}</tbody>
      </table>
    </div>`;
}

async function loadCountryDrilldown(country, countryCode, drawerRow, windowKey) {
  const container = drawerRow.querySelector(".country-drilldown-panel");
  if (!container) return;
  const window_ = windowKey || roasCountryWindowKey();
  const gen = roasCountryDrilldownGen;
  const key = countryDrilldownKey(country, countryCode, window_);
  if (roasCountryDrilldownCache[key]) {
    container.innerHTML = renderCountryDrilldown(roasCountryDrilldownCache[key]);
    return;
  }
  container.innerHTML = `<p class="country-drilldown-muted">Loading country client details…</p>`;
  roasCountryDrilldownLoading[key] = true;
  try {
    const codeParam = countryCode
      ? `&country_code=${encodeURIComponent(countryCode)}`
      : "";
    const res = await fetch(
      `/api/revenue-performance/country-detail?window=${encodeURIComponent(window_)}&country=${encodeURIComponent(country)}${codeParam}`,
      { credentials: "same-origin" }
    );
    if (!res.ok) {
      roasCountryDrilldownError[key] = true;
      container.innerHTML = `<p class="country-drilldown-muted">Country client details could not be loaded.</p>`;
      return;
    }
    const data = await res.json();
    if (gen !== roasCountryDrilldownGen) return;   // the page reloaded meanwhile
    roasCountryDrilldownCache[key] = data;
    container.innerHTML = renderCountryDrilldown(data);
  } catch (err) {
    console.error("[loadCountryDrilldown]", err);
    roasCountryDrilldownError[key] = true;
    container.innerHTML = `<p class="country-drilldown-muted">Country client details could not be loaded.</p>`;
  } finally {
    roasCountryDrilldownLoading[key] = false;
  }
}

// PR-ADS-176B: a market row opens the Dashboard's drawer (`.ctry-drawer`) under
// the country table — the row's own figures, then the closed-won deals behind
// it, lazily read once from the country-detail endpoint (the row's own
// population: same scope, same days, same exclusions — PR-ADS-176A). A row
// whose deal count is withheld has no deal list to read: the drawer says
// withheld, never "no deals". The residual never opens a drawer.
function renderCountryDrawer(r, data) {
  const opts = campBlockerOpts(data || roasCountryMart);
  const name = countryDisplayName(r.country);
  const links = [["pipeline", "Open Pipeline"], ["revenue-health", "Open Revenue Health"]]
    .filter(([page]) => dashCanNavigate(page))
    .map(([page, label]) => `<a class="ctry-drawer__link" href="#/${escapeHtml(page)}">${escapeHtml(label)} →</a>`).join("");
  const dealsWithheld = r.closed_won_deals === null || r.closed_won_deals === undefined;
  return `
    <div class="ctry-drawer__head">
      <strong>${escapeHtml(name)}</strong>${spendOwnerNote(r)}
      <span class="ctry-drawer__region">${escapeHtml(r.region || "")}</span>
      ${getBusinessVerdictBadge(r.verdict)}
      <button type="button" class="ctry-drawer__close" data-country-drawer-close aria-label="Close">×</button>
    </div>
    <dl class="ctry-drawer__summary">
      <div><dt>Spend</dt><dd>${roasCountrySpendCell(r)}</dd></div>
      <div><dt>${escapeHtml(COHORT_LEADS_LABEL + cohortThroughSuffix((data || {}).cohort))}</dt><dd>${cohortCountHtml(r.leads, (data || {}).cohort, r)}</dd></div>
      <div><dt>${escapeHtml(COHORT_SQLS_LABEL + cohortThroughSuffix((data || {}).cohort))}</dt><dd>${cohortCountHtml(r.sqls, (data || {}).cohort, r)}</dd></div>
      <div><dt>Won deals</dt><dd>${rowMetricHtml(r, "closed_won_deals", fmtCount, opts)}</dd></div>
      <div><dt>Customers</dt><dd>${rowMetricHtml(r, "customers", fmtCount, opts)}</dd></div>
      <div><dt>Won revenue</dt><dd>${rowMetricHtml(r, "won_revenue", fmtMoney, opts)}</dd></div>
      <div><dt>${escapeHtml(PERIOD_RATIO_LABEL)}</dt><dd>${rowMetricHtml(r, "period_revenue_per_ad_spend", fmtPeriodRatio, opts)}</dd></div>
    </dl>
    <div class="ctry-drawer__section">
      <div class="country-drilldown-panel">${dealsWithheld
        ? `<p class="dash-unavailable">Closed-won deals are withheld for this country in this window.</p>`
        : ""}</div>
    </div>
    <div class="ctry-drawer__actions">${links}</div>`;
}

function openCountryDrawer(idx) {
  const r = roasCountryRenderedRows[idx];
  const panel = document.querySelector("#roas-countries-table-body .ctry-leaderboard-panel");
  if (!r || !panel) return;
  let drawer = panel.querySelector(".ctry-drawer");
  if (!drawer) {
    drawer = document.createElement("div");
    drawer.className = "ctry-drawer";
    panel.appendChild(drawer);
  }
  const data = roasCountryMart;
  const windowKey = ((data || {}).window || {}).key;
  drawer.innerHTML = renderCountryDrawer(r, data);
  roasCountryExpandedKey = countryDrilldownKey(r.country, r.country_code, windowKey);
  if (r.closed_won_deals !== null && r.closed_won_deals !== undefined) {
    loadCountryDrilldown(r.country || "", r.country_code || "", drawer, windowKey);
  }
  if (drawer.scrollIntoView) drawer.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

document.addEventListener("click", (e) => {
  const close = e.target.closest("[data-country-drawer-close]");
  if (close) {
    const drawer = close.closest(".ctry-drawer");
    if (drawer) drawer.remove();
    return;
  }
  const row = e.target.closest("#roas-countries-table-body [data-country-row]");
  if (!row || e.target.closest("a")) return;
  openCountryDrawer(Number(row.getAttribute("data-country-row")));
});

document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const row = e.target.closest && e.target.closest("#roas-countries-table-body [data-country-row]");
  if (!row) return;
  e.preventDefault();
  openCountryDrawer(Number(row.getAttribute("data-country-row")));
});

// Decision-bucket filter chips (country page only) — event delegation keyed off
// the country-specific data attribute so the Campaign page is unaffected.
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-roas-country-filter]");
  if (!btn) return;
  roasCountryFilter = btn.dataset.roasCountryFilter || "all";
  renderRoasCountriesPage();
});

// PR-ADS-124: blocked Country Revenue & Spend card CTAs. "Open Revenue Health" navigates;
// the admin-only run actions navigate to Revenue Health and kick the job there so
// progress + reconciliation totals are visible. Country ROAS stays blocked until
// geo spend reconciles — these buttons fix the recovery path, not the safety rule.
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-country-cta]");
  if (!btn) return;
  const action = btn.dataset.countryCta;
  navigate("revenue-health");
  if (action === "run-geo-sync") {
    setTimeout(() => { if (typeof runGeoSyncJob === "function") runGeoSyncJob(false); }, 0);
  } else if (action === "run-spend-backfill") {
    setTimeout(() => { if (typeof runSpendBackfillJob === "function") runSpendBackfillJob(false); }, 0);
  }
});

// ── Page Help Drawer (PR-ADS-071) ─────────────────────────────────────────

let _helpDrawerOpen = false;
let _helpDrawerFocusReturn = null;

function openHelpDrawer(pageKey) {
  const resolvedPageKey = pageKey || _currentPage;
  const overlay = document.getElementById("help-drawer-overlay");
  const drawer = document.getElementById("help-drawer");
  if (!overlay || !drawer) return;
  if (_helpDrawerOpen) return;

  if (!resolvedPageKey || !PAGE_HELP_CONTENT[resolvedPageKey]) {
    console.warn("[helpDrawer] Missing help content for page:", resolvedPageKey);
    closeHelpDrawer();
    return;
  }

  const content = PAGE_HELP_CONTENT[resolvedPageKey];
  const explanation = PAGE_EXPLANATIONS[resolvedPageKey];

  const title = (explanation && explanation.title) || resolvedPageKey;

  let bodyHtml = `<h2 class="help-drawer__page-title">${escapeHtml(title)}</h2>`;

  if (content) {
    bodyHtml += `
      <div class="help-drawer__section">
        <h3 class="help-drawer__section-title">What this page is</h3>
        <p>${escapeHtml(content.what)}</p>
      </div>
      <div class="help-drawer__section">
        <h3 class="help-drawer__section-title">Where the data comes from</h3>
        <p>${escapeHtml(content.source)}</p>
      </div>
      <div class="help-drawer__section">
        <h3 class="help-drawer__section-title">How to use it</h3>
        <p>${escapeHtml(content.howToUse)}</p>
      </div>
      ${content.howItStaysCurrent ? `
      <div class="help-drawer__section">
        <h3 class="help-drawer__section-title">How it stays current</h3>
        <p>${escapeHtml(content.howItStaysCurrent)}</p>
      </div>` : ""}
      <div class="help-drawer__section">
        <h3 class="help-drawer__section-title">What not to assume</h3>
        <p>${escapeHtml(content.doNotAssume)}</p>
      </div>
      <div class="help-drawer__section">
        <h3 class="help-drawer__section-title">What to check next</h3>
        <p>${escapeHtml(content.checkNext)}</p>
      </div>`;
  }

  const body = document.getElementById("help-drawer-body");
  if (body) body.innerHTML = bodyHtml;

  _helpDrawerFocusReturn = document.activeElement;
  overlay.hidden = false;
  overlay.setAttribute("aria-hidden", "false");
  drawer.hidden = false;
  _helpDrawerOpen = true;

  // Focus the close button
  const closeBtn = document.getElementById("help-drawer-close-btn");
  if (closeBtn) closeBtn.focus();

  document.addEventListener("keydown", _helpDrawerKeyHandler);
}

function closeHelpDrawer() {
  const overlay = document.getElementById("help-drawer-overlay");
  const drawer = document.getElementById("help-drawer");

  if (overlay) {
    overlay.hidden = true;
    overlay.setAttribute("aria-hidden", "true");
  }

  if (drawer) {
    drawer.hidden = true;
  }

  _helpDrawerOpen = false;
  document.removeEventListener("keydown", _helpDrawerKeyHandler);

  const body = document.getElementById("help-drawer-body");
  if (body) {
    body.innerHTML = '<p class="empty-state">Select a page to view its help guide.</p>';
  }

  if (
    _helpDrawerFocusReturn &&
    typeof _helpDrawerFocusReturn.focus === "function" &&
    _helpDrawerFocusReturn instanceof Element &&
    document.body.contains(_helpDrawerFocusReturn)
  ) {
    _helpDrawerFocusReturn.focus();
  }

  _helpDrawerFocusReturn = null;
}

function _helpDrawerKeyHandler(e) {
  if (e.key === "Escape") {
    e.preventDefault();
    closeHelpDrawer();
    return;
  }
  if (e.key === "Tab" && _helpDrawerOpen) {
    _trapHelpDrawerFocus(e);
  }
}

function _trapHelpDrawerFocus(e) {
  const drawer = document.getElementById("help-drawer");
  if (!drawer || drawer.hidden) return;

  const focusable = Array.from(drawer.querySelectorAll(
    'a[href], button:not([disabled]), textarea, input, select, [contenteditable="true"], [tabindex]:not([tabindex="-1"])'
  )).filter((el) => !el.closest("[hidden]"));

  if (!focusable.length) {
    e.preventDefault();
    drawer.focus();
    return;
  }

  const first = focusable[0];
  const last = focusable[focusable.length - 1];

  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}
