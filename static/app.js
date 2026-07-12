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

const PAGES = ["dashboard", "reports", "campaigns", "waste", "search-terms", "ngrams", "geo", "keywords", "leads", "deals", "gclid-attribution", "opportunities", "scheduler", "health", "action-queue", "backfill", "historical-intelligence", "roas-campaigns", "roas-countries", "unit-economics", "churn-input", "revenue-health", "revenue-by-source"];

// PR-ADS-110/113: Revenue & Attribution pages. These use business-revenue
// windows (not ad-style 7d/14d/30d/60d) and a clean readiness model — the global
// ad-window bar, monitoring warning, help/explanation blocks, and any
// attribution-audit controls are suppressed here. Deals (Closed-Won Revenue
// Ledger) and Revenue Health (admin diagnostics) joined in PR-ADS-113.
// PR-ADS-134: the Dashboard is now the Executive Overview — a business-truth
// command center on business windows — so it joins the clean revenue chrome.
const REVENUE_PAGES = ["dashboard", "roas-campaigns", "roas-countries", "deals", "revenue-health", "revenue-by-source"];

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
  waste:             ["google_ads_api/search_terms"],   // waste_terms derived from search_terms
  search_terms:      ["google_ads_api/search_terms"],
  ngrams:            ["google_ads_api/search_terms"],   // n-grams derived from search_terms
  geo:               ["google_ads_api/geo"],
  keywords:          ["google_ads_api/keywords"],
  lead_quality:      ["hubspot/contacts"],
  deals:             ["hubspot/deals"],
  gclid_attribution: ["gclid/matches"],
  in_progress_leads: ["hubspot/contacts"],
  action_queue:      ["google_ads_api/campaigns", "hubspot/contacts", "hubspot/deals"],
  reports:           ["google_ads_api/campaigns", "hubspot/contacts", "hubspot/deals", "google_ads_api/search_terms"],
};

// Pages whose output is derived/computed from the source datasets above rather
// than being a direct read of the synced table.  Strip copy uses "Derived from"
// wording for these rather than "Dataset freshness:".
const DERIVED_DATASET_PAGES = new Set(["ngrams", "waste"]);

// ── Page Explanations (PR-ADS-070) ────────────────────────────────────────
//
// Each page gets a concise explanation block: what it shows, data source,
// dependencies, what empty means, and what action to take next.

const PAGE_EXPLANATIONS = {
  dashboard: {
    title: "Dashboard",
    purpose: "Executive overview of spend, closed-won revenue, pipeline movement, and action signals for one business window.",
    source: "Canonical Revenue Decision Mart — Google Ads spend truth and HubSpot closed-won revenue truth.",
    dependsOn: ["campaigns", "leads", "deals", "gclid_attribution"],
    emptyMeans: "No verified spend or closed-won revenue exists in the selected business window.",
    nextAction: "Open Revenue Health to check spend, FX, and revenue truth status."
  },
  "action-queue": {
    title: "Action Queue",
    purpose: "Ranked human-review items based on campaign, waste, geo, keyword, and data-quality signals.",
    source: "Campaigns, search terms, waste, and lead data.",
    dependsOn: ["campaigns", "search_terms", "waste_terms", "leads"],
    emptyMeans: "No queue rules crossed the current review thresholds for this window.",
    nextAction: "Check System Status to verify all pipelines are running."
  },
  reports: {
    title: "Reports",
    purpose: "Latest generated advisor report from the scheduled analysis pipeline.",
    source: "Weekly/monthly report generation jobs.",
    dependsOn: ["weekly_report"],
    emptyMeans: "No report has been generated yet. Reports appear after weekly or monthly scheduler jobs complete.",
    nextAction: "Check Data Runs to confirm scheduler has executed."
  },
  campaigns: {
    title: "Campaign Evidence",
    purpose: "Selected-window campaign evidence — genuine window totals for spend, leads, SQLs, junk and CPQL, with a factual outcome status per campaign.",
    source: "Canonical Google Ads campaign spend (native GBP + FX-safe USD) reconciled with deduplicated HubSpot lead-quality outcomes for the selected window. All-time returns genuine cumulative totals, not a scheduler snapshot.",
    dependsOn: ["campaigns", "leads"],
    emptyMeans: "No canonical spend or HubSpot lead evidence exists for the selected window. Run the daily/weekly scheduler or check System Status.",
    nextAction: "Check System Status → Google Ads API / Campaigns."
  },
  "search-terms": {
    title: "Search Terms",
    purpose: "Selected-window Search Term Universe — queries reported by Google Ads, with factual tri-state review states, waste evidence and pattern analysis.",
    source: "Durable Google Ads API search-term rows bounded by source_date; spend is reported search-term spend (stored USD), never canonical account spend.",
    dependsOn: ["search_terms"],
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
  waste: {
    title: "Flagged Waste Terms",
    purpose: "Search terms flagged for human review as potential wasted spend.",
    source: "Analysis layer derived from Search Term Universe.",
    dependsOn: ["search_terms", "waste_terms"],
    emptyMeans: "This may mean no terms were flagged, or it may mean Search Term Universe is empty. Waste detection depends on Search Term Universe — if search-term data is unavailable, no waste verdict can be produced.",
    nextAction: "Check System Status before assuming no waste exists."
  },
  geo: {
    title: "Country Performance",
    purpose: "Geographic intelligence — performance and quality by country.",
    source: "Google Ads API geo data.",
    dependsOn: ["geo"],
    emptyMeans: "No Google Ads API geo data has been synced yet for this window. Run the weekly/monthly scheduler or check System Status.",
    nextAction: "Check System Status → Google Ads API / Geo."
  },
  keywords: {
    title: "Keyword Performance",
    purpose: "Keyword-level performance metrics and quality signals.",
    source: "Google Ads API keyword data.",
    dependsOn: ["keywords"],
    emptyMeans: "No Google Ads API keyword data has been synced yet for this window. Run the weekly/monthly scheduler or check System Status.",
    nextAction: "Check System Status → Google Ads API / Keywords."
  },
  leads: {
    title: "Lead Quality",
    purpose: "HubSpot lead-quality breakdown — lifecycle stage, junk rate, and campaign attribution.",
    source: "HubSpot CRM contacts.",
    dependsOn: ["leads"],
    emptyMeans: "No HubSpot lead-quality rows found for this window. Contacts may not have synced.",
    nextAction: "Check System Status → HubSpot CRM."
  },
  deals: {
    title: "Deals",
    purpose: "HubSpot deal pipeline — stages, values, and campaign attribution.",
    source: "HubSpot CRM deals.",
    dependsOn: ["deals"],
    emptyMeans: "No deal rows found for this window. HubSpot deals may not have synced.",
    nextAction: "Check System Status → HubSpot CRM."
  },
  "gclid-attribution": {
    title: "GCLID Attribution",
    purpose: "Click-to-lead matching via GCLID — proves which Google Ads click produced which HubSpot contact.",
    source: "GCLID match engine joining Windsor + HubSpot.",
    dependsOn: ["gclid_attribution", "gclid_coverage_snapshots"],
    emptyMeans: "No GCLID attribution rows found. This may mean no matching GCLIDs exist in the selected window.",
    nextAction: "Check System Status → GCLID / Matches."
  },
  opportunities: {
    title: "In Progress Leads",
    purpose: "Leads currently in active pipeline stages (not yet closed-won or lost).",
    source: "HubSpot CRM contacts.",
    dependsOn: ["leads"],
    emptyMeans: "No in-progress leads in the selected window.",
    nextAction: "Check System Status → HubSpot CRM."
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
  "historical-intelligence": {
    title: "Historical Trends",
    purpose: "Long-term historical intelligence — period-over-period comparisons and trend analysis.",
    source: "Historical campaign, lead, and deal data.",
    dependsOn: ["historical_intelligence"],
    emptyMeans: "Not enough historical data for trend analysis, or the data has not been backfilled yet.",
    nextAction: "Check Admin Backfill to verify historical data availability."
  },
  "roas-campaigns": {
    title: "ROAS by Campaign",
    purpose: "Revenue attribution by campaign over business windows — which campaigns produce SQLs, customers, and closed-won revenue.",
    source: "Google Ads API = spend/platform evidence. HubSpot = revenue truth (closed-won deals).",
    dependsOn: ["hubspot/deals", "google_ads_api/campaigns"],
    emptyMeans: "No attributed revenue found for this business window. Spend may exist without a HubSpot closed-won match, or SQL/customer data may not be mapped to campaigns yet.",
    nextAction: "Check that HubSpot deals are synced and campaigns have Google Ads API spend data for the selected window."
  },
  "roas-countries": {
    title: "ROAS by Country",
    purpose: "Revenue attribution by country over business windows — where SQLs, customers, and closed-won revenue come from.",
    source: "Google Ads API = spend/platform evidence. HubSpot = revenue truth (closed-won deals).",
    dependsOn: ["hubspot/deals", "google_ads_api/geo"],
    emptyMeans: "No attributed revenue found for this business window. Spend may exist without a HubSpot closed-won match, or attribution may be missing/low confidence for these countries.",
    nextAction: "Check that geo spend and HubSpot deal data are synced for the selected window."
  },
  "unit-economics": {
    title: "Unit Economics",
    purpose: "Executive SaaS economics — LTV/CAC, payback, and profitability verdicts.",
    source: "Revenue Truth Layer.",
    dependsOn: ["hubspot/deals", "google_ads_api/campaigns"],
    emptyMeans: "Not enough closed-won volume to compute unit economics.",
    nextAction: "Ensure sufficient deal volume exists in the selected window."
  },
  "churn-input": {
    title: "Churn Input",
    purpose: "Admin page for manual monthly churn rate overrides used in LTV/ROAS calculations.",
    source: "Local YAML configuration.",
    dependsOn: [],
    emptyMeans: "Default churn rate is applied when no overrides are set.",
    nextAction: "Add a monthly override if the default rate (3%) does not reflect reality."
  }
};

// ── Page Help Drawer Content (PR-ADS-071) ─────────────────────────────────
//
// Extended help content for each page. Displayed in a slide-out help drawer.
// Each entry: what, source, howToUse, doNotAssume, checkNext.

const PAGE_HELP_CONTENT = {
  dashboard: {
    what: "Executive overview of Google Ads spend, HubSpot closed-won revenue, pipeline conversion, source mix, and computed action signals for one business window.",
    source: "Canonical Revenue Decision Mart (Google Ads spend truth + HubSpot closed-won revenue truth), Revenue by Source, and the closed-won deal ledger.",
    howToUse: "Pick a business window, scan the hero KPIs and decision cards, then follow the evidence links (ROAS by Campaign, Revenue by Source, Action Queue) to drill down.",
    doNotAssume: "'Unavailable' means the truth layer withheld a metric (FX, coverage, or lead-date safety) — it is never a $0. ROAS applies to Google Ads only; other sources are revenue-only.",
    checkNext: "Revenue Health for truth blockers, or Action Queue for items needing human review."
  },
  "action-queue": {
    what: "Ranked list of items requiring human review — campaigns to investigate, waste to confirm, geo anomalies, keyword issues, and data quality flags.",
    source: "Generated from campaign verdicts, waste detection, geo intelligence, keyword metrics, and data quality checks.",
    howToUse: "Work top-down by severity. Each item links to the relevant evidence page. Items are prompts for investigation, not automated actions.",
    doNotAssume: "Severity is advisory only. An empty queue means no rules crossed thresholds — it does not guarantee account health.",
    checkNext: "System Status to verify all pipelines ran, then the specific page linked from each queue item."
  },
  reports: {
    what: "Latest generated advisor report from the scheduled analysis pipeline. Shows the most recent markdown report file.",
    source: "Weekly/monthly report generation jobs produce markdown files.",
    howToUse: "Read the latest report for a narrative summary. Use the Copy button to share with stakeholders. Refresh to check for newer reports.",
    doNotAssume: "This is a point-in-time snapshot, not a live re-analysis. Data may have changed since the report was generated.",
    checkNext: "Data Runs to see when the last report was generated."
  },
  campaigns: {
    what: "Campaign Evidence — genuine selected-window totals for spend, leads, SQLs, junk and CPQL, with a factual Outcome Status per campaign (SQL producer · Junk-heavy · Spend without SQL proof · Mapping review · No outcome evidence · Data unavailable). Outcome Status is a description of the window's evidence, not a scale/cut recommendation.",
    source: "Canonical daily Google Ads API spend (native GBP + FX-safe USD — the same source as Revenue by Source) reconciled with deduplicated HubSpot lead outcomes on the HubSpot event date (contact_created_at). Lead labels are mapped to canonical Google Ads campaigns through the approved campaign-identity table; unmatched labels appear as Mapping Review rows.",
    howToUse: "Pick an Evidence Window (7d…All time; All time is genuine cumulative totals). Filter by status/outcome and sort. Click a row to open the drawer — its lead evidence aggregates across the campaign's approved aliases and reconciles exactly with the row.",
    doNotAssume: "Read-only — no Google Ads or HubSpot changes are made. Spend is canonical Google Ads truth, never a snapshot; overall CPQL uses mapped Google Ads SQLs only (unmatched / Not-Google-Ads SQLs are disclosed, never fold into it).",
    checkNext: "Campaign detail drawer for drill-down, or ROAS by Campaign for revenue attribution."
  },
  "search-terms": {
    what: "Search Terms + Patterns — the selected-window Search Term Universe with factual review states (Flagged waste = stored true · Reviewed clean = stored false · Needs review = not yet classified) and waste evidence, plus a Patterns tab of recurring 1–3-word phrases derived from the same terms.",
    source: "Durable Google Ads API search-term rows totalled by their source_date report date for the selected Evidence Window (all_time has no lower bound). Spend is reported search-term spend in the account's native currency (e.g. GBP), FX-converted to USD using the same per-date rate doctrine as canonical campaign spend. When FX is incomplete or currency provenance is unproven, USD spend is withheld (Unavailable) and native spend is shown. Reporting Coverage compares FX-safe search-term USD against FX-safe canonical campaign USD — anything else returns Unavailable.",
    howToUse: "Pick an Evidence Window, filter by campaign, review state, junk category or minimum spend, and click any row to open its evidence drawer. Switch to Patterns for word-level analysis derived from the same selected-window terms; a term can appear in several patterns, so pattern-row spends overlap and must never be summed.",
    doNotAssume: "Review states are classification facts, not business outcomes — Reviewed clean does not mean valuable, and a platform conversion is never a confirmed SQL, customer or closed-won deal. A flagged term is a human-review candidate, not an approved negative. Read-only: no negative keywords are added and nothing is written to Google Ads or HubSpot. The legacy spend_usd column in storage is NOT proven USD — it stores the raw cost_micros/1e6 value in the account's native currency.",
    checkNext: "Flagged Waste Terms for the flagged review queue, or Campaign Evidence for canonical spend and lead outcomes."
  },
  keywords: {
    what: "Keyword-level performance metrics — impressions, clicks, spend, conversions, and quality signals per keyword.",
    source: "Google Ads API keyword data.",
    howToUse: "Sort by spend or quality score. Filter to find high-spend low-quality keywords that may need attention.",
    doNotAssume: "Keyword data reflects Google Ads reporting. Quality scores are Google's metric, not an internal calculation.",
    checkNext: "Campaigns page for campaign-level context, or Search Terms to see what queries matched each keyword."
  },
  geo: {
    what: "Country-level ad performance and HubSpot lead quality shown side by side. Identify which countries produce quality leads vs. waste.",
    source: "Google Ads API geo reports merged with HubSpot lead quality by country.",
    howToUse: "Compare spend, lead count, and junk rate across countries. Look for high-spend countries with poor lead quality.",
    doNotAssume: "Country attribution uses Google Ads geographic reporting. It does not use IP geolocation or GCLID-level precision.",
    checkNext: "ROAS by Country for revenue attribution at the country level."
  },
  leads: {
    what: "HubSpot lead-quality breakdown — lifecycle stages, junk rate, campaign attribution, and quality scoring.",
    source: "HubSpot CRM contacts synced via the HubSpot connector.",
    howToUse: "Review junk rate by campaign. Identify which campaigns produce MQLs vs. junk. Use time window to track quality trends.",
    doNotAssume: "Lead quality labels come from HubSpot lifecycle stages. The system does not modify lead statuses in HubSpot.",
    checkNext: "Campaigns page to correlate lead quality with campaign spend and verdicts."
  },
  opportunities: {
    what: "Leads currently in active pipeline stages — not yet closed-won or lost. Shows in-progress pipeline activity.",
    source: "HubSpot CRM contacts filtered to active lifecycle stages.",
    howToUse: "Monitor pipeline velocity. Identify leads that are stuck in early stages or progressing toward conversion.",
    doNotAssume: "This is a snapshot of current pipeline state. It does not predict conversion probability.",
    checkNext: "Deals page for closed-won revenue, or Lead Quality for quality breakdown."
  },
  waste: {
    what: "Search terms flagged by waste detection as potential wasted spend. Grouped by junk signal and campaign evidence.",
    source: "Analysis layer derived from Search Term Universe combined with HubSpot lead quality signals.",
    howToUse: "Review flagged terms. Copy them for use as negative keywords in Google Ads (manual process). Filter by category or campaign.",
    doNotAssume: "Flagged terms are candidates for human review, not confirmed waste. The system does not push negative keywords to Google Ads.",
    checkNext: "Search Terms for full context, or Campaigns for spend impact."
  },
  deals: {
    what: "HubSpot deal pipeline — stages, values, and campaign attribution for closed-won and in-progress deals.",
    source: "HubSpot CRM deals synced via the HubSpot connector.",
    howToUse: "Review deal values by campaign to understand revenue attribution. Filter by stage or time window.",
    doNotAssume: "Deal attribution uses HubSpot's native campaign field. It does not re-attribute deals using click-level GCLID data (that requires GCLID bridge).",
    checkNext: "ROAS by Campaign for campaign-level profitability, or Unit Economics for LTV/CAC."
  },
  "roas-campaigns": {
    what: "Revenue Truth ROAS by campaign — shows which campaigns produce profitable customers based on closed-won HubSpot revenue and Google Ads API spend.",
    source: "HubSpot closed-won deal revenue + Google Ads API campaign spend. This does NOT use Google Ads conversion value.",
    howToUse: "Compare campaigns by LTV ROAS, ACV ROAS, and verdict. Use the window selector to analyse different time periods. SCALE campaigns are profitable; CUT campaigns are losing money.",
    doNotAssume: "This is HubSpot revenue truth, not Google Ads conversion tracking. Revenue comes from actual closed-won deals, not estimated conversion values. INSUFFICIENT_DATA means not enough signal to judge.",
    checkNext: "Unit Economics for portfolio-level LTV/CAC, or ROAS by Country for geographic breakdown."
  },
  "roas-countries": {
    what: "Country-level Revenue Truth ROAS — estimated until GCLID attribution is fully wired. Shows directional profitability by geography.",
    source: "HubSpot closed-won deal revenue + Google Ads API geo spend. Country ROAS is an estimate because deal-to-country mapping is approximate without full GCLID coverage.",
    howToUse: "Use for directional diagnosis — identify countries that are clearly profitable or clearly losing money. Do not use for precise budget allocation until GCLID attribution is fully wired.",
    doNotAssume: "These are estimates, not exact figures. Country attribution precision will improve as GCLID coverage increases. Do not make final budget decisions based solely on these numbers.",
    checkNext: "GCLID Attribution page to check coverage readiness, or ROAS by Campaign for campaign-level precision."
  },
  "gclid-attribution": {
    what: "GCLID click-to-lead matching — a readiness and confidence audit showing how well Google Ads clicks can be traced to HubSpot leads.",
    source: "GCLID match engine joining Windsor click data with HubSpot contact records.",
    howToUse: "Review match rates, coverage percentages, and confidence tiers. This page audits attribution readiness — it does not implement a GCLID-to-revenue bridge.",
    doNotAssume: "This is a readiness/confidence audit only. It does NOT implement actual GCLID bridge attribution, OCT upload, or offline conversion tracking. It measures how ready the data is for those future capabilities.",
    checkNext: "ROAS by Country to see how estimates will improve with better coverage, or System Status for GCLID pipeline health."
  },
  "unit-economics": {
    what: "Executive SaaS economics — LTV, CAC, LTV/CAC ratio, payback months, and profitability verdicts at the portfolio level.",
    source: "Revenue Truth Layer computed from HubSpot deals + Google Ads API campaigns + churn rate configuration.",
    howToUse: "Review overall LTV/CAC and payback. Compare with industry benchmarks. A ratio above 3:1 generally indicates healthy unit economics.",
    doNotAssume: "LTV calculations use the configured churn rate (default 3%). If actual churn differs, LTV will be inaccurate. Adjust via Churn Input page.",
    checkNext: "Churn Input to verify/update churn rate, or ROAS by Campaign for campaign-level detail."
  },
  scheduler: {
    what: "Scheduler job status — run history, next scheduled runs, and manual trigger buttons for daily/weekly/monthly jobs.",
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
  },
  "churn-input": {
    what: "Admin page for manual monthly churn rate overrides. Churn rate is used in LTV and ROAS calculations.",
    source: "Local YAML configuration file. Not synced from or written to any external system.",
    howToUse: "Enter a month (YYYY-MM) and the observed monthly churn rate (0–1). The override applies to LTV calculations for that month onward.",
    doNotAssume: "This updates local configuration ONLY. It does NOT write to HubSpot, Google Ads, or any external service. Changes affect future LTV/ROAS calculations locally.",
    checkNext: "Unit Economics to see updated LTV calculations, or ROAS by Campaign to see how churn affects campaign verdicts."
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
  dashboard:               ["google_ads_api/campaigns", "hubspot/contacts", "hubspot/deals", "google_ads_api/search_terms"],
  "action-queue":          ["google_ads_api/campaigns", "google_ads_api/search_terms", "hubspot/contacts"],
  reports:                 ["google_ads_api/campaigns", "hubspot/contacts", "hubspot/deals"],
  campaigns:               ["google_ads_api/canonical_spend", "hubspot/contacts"],
  waste:                   ["google_ads_api/search_terms"],
  "search-terms":          ["google_ads_api/search_terms"],
  ngrams:                  ["google_ads_api/search_terms"],
  geo:                     ["google_ads_api/geo"],
  keywords:                ["google_ads_api/keywords"],
  leads:                   ["hubspot/contacts"],
  deals:                   ["hubspot/deals"],
  "gclid-attribution":     ["gclid/matches"],
  opportunities:           ["hubspot/contacts"],
  scheduler:               [],
  health:                  [],
  backfill:                [],
  "historical-intelligence": [],
  "roas-campaigns":          ["hubspot/deals", "google_ads_api/campaigns"],
  "roas-countries":          ["hubspot/deals", "google_ads_api/geo"],
  "unit-economics":          ["hubspot/deals", "google_ads_api/campaigns"],
  "churn-input":             [],
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
// countries=geo, lead-quality=leads, in-progress-leads=opportunities,
// flagged-waste-terms=waste.) N-Grams is derived Search Term evidence, so it
// uses the same Evidence window dropdown (its request is window-driven).
const EVIDENCE_PAGES = [
  "campaigns", "search-terms", "ngrams", "keywords", "geo",
  "leads", "opportunities", "waste",
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

// Geo page state
let geoMergedRows = [];

// Keywords page state
let keywordRows = [];
let keywordLoadStatus = "idle"; // idle | loading | ok | empty | db_unavailable | error

// Action Queue page state
let actionQueueItems = [];
let actionQueueStatus = "idle"; // idle | loading | ok | empty | db_unavailable | error

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

// Campaign drawer attribution preview state
let campaignAttributionRows = [];
let campaignAttributionStatus = "idle"; // idle | loading | ok | empty | db_unavailable | error
let campaignAttributionQuality = null;
let campaignAttributionQualityStatus = "idle"; // idle | loading | ok | empty | db_unavailable | error

// GCLID Attribution page prefill (set when navigating from campaign drawer)
let gclidAttributionPrefill = null;

// N-Gram page prefill (set when navigating from campaign drawer)
let ngramsPrefill = null;

// Keyword / Waste page prefill (set when navigating from the campaign drawer,
// PR-ADS-142). The loaders apply the campaign filter if the input exists.
let keywordsPrefill = null;
let wastePrefill = null;

// Campaign drawer N-Gram drilldown state
let drawerNgramRows = [];
let drawerNgramStatus = "idle"; // idle | loading | ok | empty | db_unavailable | error

// Reports page state
let latestReportMarkdown = "";
let latestReportMeta = null;
let reportLoadStatus = "idle"; // idle | loading | ok | empty | error
let reportCopyFeedbackTimer = null;

// Dataset Freshness state
let datasetFreshnessRows = [];
let datasetFreshnessStatus = "idle"; // idle | loading | ok | empty | db_unavailable | error

// Per-dataset freshness cache — populated by loadDatasetFreshness() and used by
// renderPageDatasetFreshness().  Keyed by "source/dataset" (e.g. "google_ads_api/campaigns").
let _datasetFreshnessByKey = {};

// Historical Intelligence page state
let hiRows = [];
let hiSummary = null;
let hiStatus = "idle"; // idle | loading | ok | empty | insufficient_data | db_unavailable | error

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
  } else if (canonicalStatus === "data_available_latest_sync_failed") {
    stateType = "degraded_sync_failed";
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
    "stale_and_empty",
    "not_run_no_upstream_data",
    "blocked_by_dependency",
    "dependency_blocked",
    "fresh_but_empty",
    "empty_success",
    "data_available_latest_sync_failed",
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
  // Close campaign drawer when selected reporting window changes to avoid stale detail.
  closeCampaignDrawer();
  // Reload current page with new window
  if (_currentPage) loadPage(_currentPage);
  loadDataFreshness();
}

function updateWindowControls(days) {
  document.querySelectorAll(".time-range-btn").forEach((btn) => {
    btn.classList.toggle("active", parseInt(btn.dataset.days, 10) === days);
  });
  // Sync any local ROAS/unit-economics window dropdowns to match global
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
  // Close the campaign drawer so stale detail from the previous window is dropped.
  closeCampaignDrawer();
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

async function fetchText(path) {
  const res = await fetch(path, { credentials: "same-origin" });
  if (res.status === 401) {
    showLogin();
    throw new Error("Authentication required");
  }
  if (res.status === 403) throw new Error("HTTP 403");
  if (res.status === 404) {
    const err = new Error("Not found");
    err.status = 404;
    throw err;
  }
  if (!res.ok) {
    throw new Error(`Request failed: ${res.status}`);
  }
  return res.text();
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
  // Ensure the drawer/overlay are hidden so they don't block the login screen.
  closeCampaignDrawer();
}

function showApp(user) {
  document.getElementById("login-screen").style.display = "none";
  document.getElementById("app").style.display = "flex";
  _currentUser = user;
  applySidebarUser(user);
  // Show/hide System Health nav item
  const healthNav = document.getElementById("nav-health-item");
  if (healthNav) healthNav.hidden = user.role !== "admin";
  // Show/hide Historical Backfill nav item
  const backfillNav = document.getElementById("nav-backfill-item");
  if (backfillNav) backfillNav.hidden = user.role !== "admin";
  // Show/hide Churn Input nav item
  const churnInputNav = document.getElementById("nav-churn-input-item");
  if (churnInputNav) churnInputNav.hidden = user.role !== "admin";
  // Show/hide Revenue Health nav item (admin-only — PR-ADS-113)
  const revenueHealthNav = document.getElementById("nav-revenue-health-item");
  if (revenueHealthNav) revenueHealthNav.hidden = user.role !== "admin";
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
  "countries": "geo",
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
const DASHBOARD_TABS = ["overview", "revenue", "channels", "campaigns", "countries", "deals"];

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
  // Same for the Search Terms Patterns deep link (#/search-terms?tab=patterns).
  // The legacy #/ngrams route also lands on the Patterns tab (PR-ADS-144).
  if (page === "search-terms") {
    const raw = window.location.hash || "";
    const q = raw.split("?")[1] || "";
    if (raw.startsWith("#/ngrams") || new URLSearchParams(q).get("tab") === "patterns") {
      hash = `${hash}?tab=patterns`;
    }
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
  if (!PAGES.includes(page)) {
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
  // Role enforcement: churn-input page is admin-only
  if (page === "churn-input" && (!_currentUser || _currentUser.role !== "admin")) {
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

  // Update active nav item
  document.querySelectorAll(".nav-item").forEach((el) => {
    el.classList.toggle("active", el.dataset.page === page);
  });

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
    case "reports":       loadReports(); loadRevenueSnapshotHistory(); break;
    case "campaigns":     loadCampaigns();                          break;
    case "waste":         loadWaste();                              break;
    case "search-terms":  loadSearchTermsEvidence();                break;
    case "geo":           loadGeo();                                break;
    case "keywords":      loadKeywords();      break;
    case "leads":         loadLeads();         break;
    case "deals":         loadDeals();         break;
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
      loadAttributionConfidenceSummary();
      break;
    case "opportunities": loadOpportunities(); break;
    case "scheduler":     loadScheduler();     break;
    case "health":        loadHealth();        break;
    case "action-queue":  loadActionQueue();   break;
    case "backfill":      loadBackfillStatus(); break;
    case "historical-intelligence": loadHistoricalIntelligence(); break;
    case "roas-campaigns":  loadRoasCampaigns();  break;
    case "roas-countries":  loadRoasCountries();  break;
    case "unit-economics":  loadUnitEconomics();  break;
    case "churn-input":     loadChurnInput();     break;
    case "revenue-health":  loadRevenueHealth();  break;
    case "revenue-by-source": loadRevenueBySource(); break;
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
  const runType    = latestRun.run_type || "unknown";
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
  } else if (ageDays > _staleAfterDays) {
    statusEl.textContent = `Latest recorded run is stale · ${dateStr} · ${runType} · ${status}`;
    statusEl.className   = "freshness-status freshness-warning";
  } else {
    statusEl.textContent = `Latest recorded run · ${dateStr} · ${runType} · ${status}`;
    statusEl.className   = "freshness-status freshness-ok";
  }
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
// Sections that depend on weekly/monthly data (campaigns, waste, keywords,
// geo, lead_quality, deals, gclid_attribution, action_queue, search_terms,
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
  const runType   = run.run_type || "unknown";
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
    hubspot: "HubSpot",
    gclid: "GCLID Attribution",
    windsor: "Windsor legacy",
  };
  return names[source] || escapeHtml(source);
}

function _datasetDisplayName(key) {
  const labels = {
    "google_ads_api/campaigns":    "Campaign data",
    "google_ads_api/search_terms": "Search-term data",
    "google_ads_api/keywords":     "Keyword data",
    "google_ads_api/geo":          "Geo data",
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
    waste:  "Derived from search terms",
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
    || s === "empty_success"
  ).length;
  const errorCount   = statuses.filter((s) =>
    s === "failed" || s === "stale_and_empty" || s === "db_unavailable"
    || s === "failed_no_data" || s === "not_run_no_upstream_data" || s === "blocked_by_dependency"
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
      // PR-ADS-095 refined states
      data_available_latest_sync_failed: "Degraded",
      failed_no_data:                    "Failed",
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

function updateReportActionButtons() {
  const copyBtn    = document.getElementById("report-copy-btn");
  const refreshBtn = document.getElementById("report-refresh-btn");
  const hasReport  = reportLoadStatus === "ok" && !!latestReportMarkdown.trim();
  if (copyBtn)    copyBtn.disabled = !hasReport || reportLoadStatus === "loading";
  if (refreshBtn) refreshBtn.disabled = reportLoadStatus === "loading";
}

async function loadReports() {
  const metaEl = document.getElementById("reports-meta");
  const bodyEl = document.getElementById("reports-viewer-body");

  // Cancel any pending copy-feedback timeout before resetting state.
  if (reportCopyFeedbackTimer) {
    clearTimeout(reportCopyFeedbackTimer);
    reportCopyFeedbackTimer = null;
  }

  reportLoadStatus = "loading";
  latestReportMarkdown = "";
  latestReportMeta = null;
  updateReportActionButtons();

  if (metaEl) metaEl.innerHTML = "";
  if (bodyEl) bodyEl.innerHTML = `<p class="empty-state">Loading report…</p>`;

  // Fetch metadata (non-fatal — raw report can still show without it).
  try {
    latestReportMeta = await fetchJSON("/reports/latest");
  } catch (_) {
    latestReportMeta = null;
  }

  // If metadata explicitly says no report exists, skip raw fetch.
  if (latestReportMeta && latestReportMeta.exists === false) {
    reportLoadStatus = "empty";
    _renderReportsMeta(latestReportMeta);
    updateReportActionButtons();
    if (bodyEl) {
      bodyEl.innerHTML = `
        <p class="empty-state">No generated report found yet.</p>
        <p class="empty-state-sub">Reports appear after weekly or monthly scheduler jobs complete.</p>`;
    }
    return;
  }

  // Fetch raw markdown body.
  try {
    latestReportMarkdown = await fetchText("/reports/latest/raw");
    reportLoadStatus = latestReportMarkdown.trim() ? "ok" : "empty";
  } catch (err) {
    // 404 means no report on disk — treat as empty state, not a hard error.
    reportLoadStatus = err.status === 404 ? "empty" : "error";
    latestReportMarkdown = "";
  }

  _renderReportsMeta(latestReportMeta);
  updateReportActionButtons();

  if (!bodyEl) return;

  if (reportLoadStatus === "error") {
    bodyEl.innerHTML = `<p class="empty-state">Could not load latest report. Check API health or run status.</p>`;
  } else if (reportLoadStatus === "empty") {
    bodyEl.innerHTML = `
      <p class="empty-state">No generated report found yet.</p>
      <p class="empty-state-sub">Reports appear after weekly or monthly scheduler jobs complete.</p>`;
  } else {
    bodyEl.innerHTML = `<pre class="report-markdown">${escapeHtml(latestReportMarkdown)}</pre>`;
  }
}

function _renderReportsMeta(meta) {
  const metaEl = document.getElementById("reports-meta");
  if (!metaEl) return;

  const filename    = meta && meta.filename    ? meta.filename    : null;
  const generatedAt = meta && meta.generated_at ? meta.generated_at : null;
  const path        = meta && meta.path        ? meta.path        : null;
  const reportType  = meta && meta.report_type ? meta.report_type : null;

  // Report is available if metadata says so OR if the raw body loaded successfully.
  const hasReportBody = !!(latestReportMarkdown && latestReportMarkdown.trim());
  const exists = meta && meta.exists === true || hasReportBody;

  metaEl.innerHTML = `
    <div class="report-meta-card">
      <div class="report-meta-card__label">Report file</div>
      <div class="report-meta-card__value">${filename ? escapeHtml(filename) : "—"}</div>
    </div>
    <div class="report-meta-card">
      <div class="report-meta-card__label">Generated</div>
      <div class="report-meta-card__value">${generatedAt ? fmtDate(generatedAt) : "—"}</div>
    </div>
    <div class="report-meta-card">
      <div class="report-meta-card__label">Source</div>
      <div class="report-meta-card__value">${path ? escapeHtml(path) : "outputs/*.md"}</div>
    </div>
    <div class="report-meta-card">
      <div class="report-meta-card__label">Type / Status</div>
      <div class="report-meta-card__value">${reportType ? escapeHtml(reportType) : "—"} · ${exists ? "Available" : "Unavailable"}</div>
    </div>`;
}

function copyLatestReport() {
  const btn = document.getElementById("report-copy-btn");
  const origHTML = btn ? btn.innerHTML : null;

  const showFeedback = (success) => {
    if (!btn) return;
    btn.innerHTML = success
      ? `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg> Copied!`
      : `Copy failed`;
    btn.disabled = true;
    reportCopyFeedbackTimer = setTimeout(() => {
      reportCopyFeedbackTimer = null;
      if (!btn || origHTML === null) return;
      btn.innerHTML = origHTML;
      // Re-enable only if the report is still loaded — guard against a concurrent refresh.
      btn.disabled = !(reportLoadStatus === "ok" && !!latestReportMarkdown.trim());
    }, 2000);
  };

  if (!latestReportMarkdown) return;

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(latestReportMarkdown).then(
      () => showFeedback(true),
      () => showFeedback(false),
    );
  } else {
    const ta = document.createElement("textarea");
    ta.value = latestReportMarkdown;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) { /* ignore */ }
    document.body.removeChild(ta);
    showFeedback(ok);
  }
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
// Ordinal ramp for the funnel stages (validated with --ordinal).
const DASH_FUNNEL_RAMP = ["#6db8f8", "#3f9df0", "#1a80cf", "#0b5f92"];

// Metric valence for delta chips: up is good for revenue/pipeline, neutral for
// spend (more spend is neither win nor loss without revenue proof).
const DASH_DELTA_VALENCE = {
  spend_usd: "neutral",
  revenue_usd: "up-good",
  sqls: "up-good",
  customers: "up-good",
  roas: "up-good",
  // PR-ADS-135 revenue-tab metric keys.
  closed_won_revenue_usd: "up-good",
  average_deal_value_usd: "up-good",
  // PR-ADS-136 channels-tab metric keys.
  total_customers: "up-good",
  total_sqls: "up-good",
  // PR-ADS-137 campaigns-tab metric keys.
  won_revenue_usd: "up-good",
  verified_spend_usd: "neutral",
  // PR-ADS-138 countries-tab metric key.
  geo_roas: "up-good",
  // PR-ADS-139 deals-tab metric keys.
  closed_won_customers: "up-good",
  sql_to_customer_rate: "up-good",
};

// ── Dashboard inner-tab controller (PR-ADS-135) ───────────────────────────
// Overview is the default; Revenue is linkable via #/dashboard?tab=revenue.
// Both roots reuse the same PR-134 design system — no new visual language.

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
  const channelsRoot = document.getElementById("dashboard-channels-root");
  const campaignsRoot = document.getElementById("dashboard-campaigns-root");
  const countriesRoot = document.getElementById("dashboard-countries-root");
  const dealsRoot = document.getElementById("dashboard-deals-root");
  if (overviewRoot) overviewRoot.hidden = target !== "overview";
  if (revenueRoot) revenueRoot.hidden = target !== "revenue";
  if (channelsRoot) channelsRoot.hidden = target !== "channels";
  if (campaignsRoot) campaignsRoot.hidden = target !== "campaigns";
  if (countriesRoot) countriesRoot.hidden = target !== "countries";
  if (dealsRoot) dealsRoot.hidden = target !== "deals";

  if (options.updateHash !== false) {
    // Overview is the default → keep the URL clean; other tabs are bookmarkable.
    const hash = target === "overview" ? "#/dashboard" : `#/dashboard?tab=${target}`;
    if (window.location.hash !== hash) history.replaceState(null, "", hash);
  }

  if (target === "revenue") loadDashboardRevenue();
  else if (target === "channels") loadDashboardChannels();
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
    ${renderDashFunnel(d)}
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

// Admin-gated routes never render as live links for viewers — navigate() would
// silently bounce them back to the dashboard, which reads as a broken link.
const DASH_ADMIN_PAGES = ["revenue-health", "health", "backfill", "churn-input"];

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

  const sqlRate = k.sql_rate !== null && k.sql_rate !== undefined
    ? `${(k.sql_rate * 100).toFixed(1)}% of leads`
    : "SQL rate unavailable";
  const customerRate = k.customer_rate !== null && k.customer_rate !== undefined
    ? `${(k.customer_rate * 100).toFixed(1)}% of leads`
    : "";

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
      value: dashValue(k.closed_won_revenue_usd, fmtMoney),
      sub: "USD · HubSpot truth",
      delta: dashDeltaChip("revenue_usd", pc),
      spark: revenueSpark,
      source: "HubSpot Closed-Won",
      ok: k.closed_won_revenue_usd !== null && k.closed_won_revenue_usd !== undefined,
    },
    {
      key: "customers",
      label: "Customers",
      value: dashValue(k.customers, fmtCount),
      sub: customerRate || "Closed-won deals",
      delta: dashDeltaChip("customers", pc),
      spark: "",
      source: "HubSpot Closed-Won",
      ok: k.customers !== null && k.customers !== undefined,
    },
    {
      key: "sqls",
      label: "SQLs",
      value: dashValue(k.sqls, fmtCount),
      sub: sqlRate,
      delta: dashDeltaChip("sqls", pc),
      spark: "",
      source: "HubSpot qualified leads",
      ok: k.sqls !== null && k.sqls !== undefined,
    },
    {
      key: "roas",
      label: "Google Ads ROAS",
      value: dashValue(k.google_ads_roas, fmtRoasMultiple),
      sub: k.google_ads_roas !== null && k.google_ads_roas !== undefined
        ? "Verified spend + revenue" : "Needs verified spend, FX, revenue",
      delta: dashDeltaChip("roas", pc),
      spark: "",
      source: "Google Ads only",
      ok: k.google_ads_roas !== null && k.google_ads_roas !== undefined,
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
      Window totals — Revenue: ${dashValue(k.closed_won_revenue_usd, fmtMoney)} ·
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
      ["Customers", p.customers, null],
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

function renderDashFunnel(d) {
  const k = d.kpis || {};
  const pc = d.period_change || {};
  // Leads has no period-change metric (the contract compares spend / revenue /
  // sqls / customers / roas) — its stage simply carries no delta chip.
  const stages = [
    { label: "Leads", value: k.leads, fmt: fmtCount, source: "HubSpot contacts", metric: null },
    { label: "SQLs", value: k.sqls, fmt: fmtCount, source: "Qualified", metric: "sqls" },
    { label: "Customers", value: k.customers, fmt: fmtCount, source: "Closed-won deals", metric: "customers" },
    { label: "Closed-Won Revenue", value: k.closed_won_revenue_usd, fmt: fmtMoney, source: "USD · HubSpot", metric: "revenue_usd" },
  ];
  const conversions = [
    dashConversion(k.sqls, k.leads),
    dashConversion(k.customers, k.sqls),
    null, // revenue is a value, not a count conversion
  ];

  const cells = stages.map((s, i) => {
    const conv = i > 0 ? conversions[i - 1] : null;
    const convChip = i === 0 ? "" : (
      conv === null
        ? '<span class="dash-funnel__conv dash-funnel__conv--muted" aria-hidden="true">→</span>'
        : `<span class="dash-funnel__conv">→ ${escapeHtml(conv)}</span>`
    );
    const delta = s.metric ? `<div class="dash-funnel__stage-delta">${dashDeltaChip(s.metric, pc)}</div>` : "";
    return `
      ${convChip}
      <div class="dash-funnel__stage" style="--funnel-accent:${DASH_FUNNEL_RAMP[i]}">
        <div class="dash-funnel__stage-label">${escapeHtml(s.label)}</div>
        <div class="dash-funnel__stage-value">${dashValue(s.value, s.fmt)}</div>
        <div class="dash-funnel__stage-sub">${escapeHtml(s.source)}</div>
        ${delta}
      </div>`;
  }).join("");

  return `
    <section class="dash-panel dash-funnel-panel dash-anim" aria-label="Pipeline funnel">
      <div class="dash-panel__header">
        <div>
          <h3 class="dash-panel__title">Pipeline</h3>
          <p class="dash-panel__sub">Leads → SQLs → Customers → Closed-Won Revenue</p>
        </div>
      </div>
      <div class="dash-funnel">${cells}</div>
    </section>`;
}

// Stage-to-stage conversion; null (not "0%") when either side is unavailable.
function dashConversion(numerator, denominator) {
  if (numerator === null || numerator === undefined) return null;
  if (denominator === null || denominator === undefined || denominator <= 0) return null;
  return `${((numerator / denominator) * 100).toFixed(1)}%`;
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
      ((s.customers || 0) > 0 || (s.revenue_usd || 0) > 0 || (s.leads || 0) > 0);
    // "Unclassified / Needs Review" carries its flag as a chip — keep the
    // label itself short so neither is lost to ellipsis truncation.
    const label = s.key === "unclassified" ? "Unclassified" : (s.label || s.key);
    // No ROAS on mix rows (not even Google Ads) — the hero KPI carries the one
    // canonical, safety-gated ROAS on this page.
    const custPart = s.customers === null || s.customers === undefined
      ? "" : ` <small>· ${escapeHtml(fmtCount(s.customers))} cust</small>`;
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

function dashSignalBody(key, s) {
  if (key === "best_campaign" || key === "best_country") {
    const roas = s.roas !== null && s.roas !== undefined ? ` · ${fmtRoasMultiple(s.roas)}` : "";
    return {
      value: s.label,
      line: `${fmtMoney(s.revenue_usd)} closed-won · ${fmtCount(s.customers)} customer${(s.customers || 0) === 1 ? "" : "s"}${roas}`,
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
      line: `${fmtCount(s.campaign_count)} campaign${(s.campaign_count || 0) === 1 ? "" : "s"} with spend but no SQLs · top: ${s.top_campaign || "—"}`,
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
        <span class="dash-signal__cta" aria-hidden="true">View evidence →</span>
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
    ${renderRevConversionStrip(d)}
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
  const largestDealCompany = (d.top_deals || [])[0] && (d.top_deals || [])[0].company;

  const cards = [
    {
      key: "revenue",
      label: "Closed-Won Revenue",
      value: dashValue(k.closed_won_revenue_usd, fmtMoney),
      sub: "USD · HubSpot closed-won",
      delta: dashDeltaChip("closed_won_revenue_usd", pc),
      source: "HubSpot Closed-Won",
      ok: k.closed_won_revenue_usd !== null && k.closed_won_revenue_usd !== undefined,
    },
    {
      key: "customers",
      label: "Customers",
      value: dashValue(k.customers, fmtCount),
      sub: "Closed-won deals",
      delta: dashDeltaChip("customers", pc),
      source: "HubSpot Closed-Won",
      ok: k.customers !== null && k.customers !== undefined,
    },
    {
      key: "avg",
      label: "Average Deal Value",
      value: dashValue(k.average_deal_value_usd, fmtMoney),
      sub: "Revenue ÷ customers",
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
      value: k.sql_to_customer_rate === null || k.sql_to_customer_rate === undefined
        ? '<span class="dash-unavailable">Unavailable</span>'
        : `${(k.sql_to_customer_rate * 100).toFixed(1)}%`,
      sub: k.sqls === null || k.sqls === undefined
        ? "SQLs unavailable" : `${escapeHtml(fmtCount(k.sqls))} SQLs`,
      delta: '<span class="dash-delta dash-delta--none">Window rate</span>',
      source: "HubSpot qualified leads",
      ok: k.sql_to_customer_rate !== null && k.sql_to_customer_rate !== undefined,
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
        <p class="dash-panel__sub">More customers, or one big deal?${bucketLabel ? ` · ${bucketLabel}` : ""}</p>
      </div>
      <div class="dash-legend">
        <span class="dash-legend__item"><span class="dash-legend__swatch" style="background:${DASH_SERIES_COLORS.revenue}"></span>Revenue</span>
        <span class="dash-legend__item"><span class="rev-legend-badge">N</span>Customers / period</span>
      </div>
    </div>
    ${note}
    ${revMomentumChartSVG(trend)}
    <p class="dash-chart-caption">
      Window total revenue: ${dashValue(k.closed_won_revenue_usd, fmtMoney)} across
      ${dashValue(k.customers, fmtCount)} customers · Avg ${dashValue(k.average_deal_value_usd, fmtMoney)}
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
    const hasCustomers = (p.customers !== null && p.customers !== undefined && p.customers > 0);
    const v = p.revenue_usd || 0;
    const h = Math.max(0, baseline - yPos(v));
    const x = xCenter(i) - barW / 2, y = yPos(v), r = Math.min(4, barW / 2, h || 4);
    // A bucket with customers but null/zero summed revenue (a permitted null
    // amount — never faked as $0) still shows its customer count at the
    // baseline, so real customer activity is never hidden from the chart.
    if (h === 0) {
      return hasCustomers
        ? `<text x="${xCenter(i).toFixed(1)}" y="${(baseline - 6).toFixed(1)}" text-anchor="middle" class="rev-count-label">${escapeHtml(String(p.customers))}</text>` : "";
    }
    const countLabel = hasCustomers
      ? `<text x="${xCenter(i).toFixed(1)}" y="${(y - 6).toFixed(1)}" text-anchor="middle" class="rev-count-label">${escapeHtml(String(p.customers))}</text>` : "";
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
     ["Customers", p.customers === null || p.customers === undefined ? "Unavailable" : String(p.customers), null],
     ["Deals", p.deals === null || p.deals === undefined ? "Unavailable" : String(p.deals), null]].forEach(([label, val, color]) => {
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

function renderRevConversionStrip(d) {
  const k = d.kpis || {};
  const rate = k.sql_to_customer_rate === null || k.sql_to_customer_rate === undefined
    ? '<span class="dash-unavailable">Unavailable</span>'
    : `${(k.sql_to_customer_rate * 100).toFixed(1)}%`;
  const stages = [
    { label: "SQLs", value: dashValue(k.sqls, fmtCount), sub: "Qualified leads" },
    { label: "Customers", value: dashValue(k.customers, fmtCount), sub: "Closed-won deals" },
    { label: "Closed-Won Revenue", value: dashValue(k.closed_won_revenue_usd, fmtMoney), sub: "USD · HubSpot" },
    { label: "Avg Deal Value", value: dashValue(k.average_deal_value_usd, fmtMoney), sub: "Per customer" },
  ];
  const conns = [
    rate,
    k.revenue_per_sql_usd === null || k.revenue_per_sql_usd === undefined
      ? '<span class="dash-unavailable">Unavailable</span>' : `${escapeHtml(fmtMoney(k.revenue_per_sql_usd))} / SQL`,
    "",
  ];
  const cells = stages.map((s, i) => {
    const conv = i > 0 ? conns[i - 1] : null;
    const chip = i === 0 ? "" : `<span class="dash-funnel__conv">→ ${conv}</span>`;
    return `${chip}
      <div class="dash-funnel__stage" style="--funnel-accent:${DASH_FUNNEL_RAMP[i]}">
        <div class="dash-funnel__stage-label">${escapeHtml(s.label)}</div>
        <div class="dash-funnel__stage-value">${s.value}</div>
        <div class="dash-funnel__stage-sub">${escapeHtml(s.sub)}</div>
      </div>`;
  }).join("");
  return `
    <section class="dash-panel dash-funnel-panel dash-anim" aria-label="Customer conversion">
      <div class="dash-panel__header">
        <div>
          <h3 class="dash-panel__title">Customer Efficiency</h3>
          <p class="dash-panel__sub">SQLs → Customers → Closed-Won Revenue → Avg Deal Value</p>
        </div>
      </div>
      <div class="dash-funnel">${cells}</div>
    </section>`;
}

function renderRevBreakdownPanel(title, sub, rows, nameKey, targetPage, opts) {
  opts = opts || {};
  const body = !rows || !rows.length
    ? '<p class="rev-breakdown-empty">No attributed revenue in this window.</p>'
    : `<ul class="rev-breakdown-list">${rows.map((r) => {
        const extra = opts.spendConnected
          ? `<span class="rev-breakdown-row__status">${escapeHtml(r.status || "")}</span>`
          : revAttrBadge(r.attribution_status);
        const custDeals = opts.spendConnected
          ? `${dashValue(r.customers, fmtCount)} cust`
          : `${dashValue(r.customers, fmtCount)} cust · ${dashValue(r.deals, fmtCount)} deals`;
        return `
        <li class="rev-breakdown-row">
          <span class="rev-breakdown-row__name" title="${escapeHtml(r[nameKey] || "")}">${escapeHtml(r[nameKey] || "—")}</span>
          <span class="rev-breakdown-row__value">${dashValue(r.revenue_usd, fmtMoney)}</span>
          <span class="rev-breakdown-row__meta">${custDeals}</span>
          ${extra}
        </li>`;
      }).join("")}</ul>`;
  const link = dashCanNavigate(targetPage)
    ? `<a class="rev-breakdown-link" href="#/${escapeHtml(targetPage)}">Open evidence →</a>`
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
  return `
    <div class="rev-breakdown-grid">
      ${renderRevBreakdownPanel("Revenue by Campaign", "Top 5 by closed-won revenue",
        b.by_campaign, "campaign", "roas-campaigns", {})}
      ${renderRevBreakdownPanel("Revenue by Source", "Top 5 · Google Ads spend-connected only",
        b.by_source, "source_group", "revenue-by-source", { spendConnected: true })}
      ${renderRevBreakdownPanel("Revenue by Country", "Top 5 by closed-won revenue",
        b.by_country, "country", "roas-countries", {})}
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
      <p class="rev-conc-foot">${dashValue(c.customers, fmtCount)} customers this window</p>
    </div>`;
}

const REV_DEAL_COLUMNS = [
  ["company", "Company"], ["company_id", "Company ID"], ["main_contact", "Main Contact"],
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
        <strong>${escapeHtml(deal.company || "Deal detail")}</strong>
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

// ── Dashboard — Channels & Platforms tab (PR-ADS-136) ──────────────────────
//
// Third dashboard tab. Reuses the PR-134/135 design system end to end (command
// header, tabs, business-window selector, KPI cards, chart shell, decision
// cards, truth footer, loading/refetch/unavailable states) — NO new visual
// language. It answers: which channels and platforms produce SQLs, customers,
// and closed-won revenue — and which are revenue-only because spend is not
// connected? Truth doctrine is identical: only Google Ads / Paid Search is
// spend-connected and ROAS-eligible; every other channel is revenue/SQL/customer
// attribution only — spend and ROAS render "Unavailable — no connected spend
// source", never a fabricated Meta/LinkedIn/Organic spend or ROAS, never a $0.

let dashChannels = null;
let dashChannelsStatus = "loading";
// Channel momentum metric toggle — Customers is the executive default.
let _chanMomentumMetric = "customers";

// Executive channel palette (8 buckets). Presentation only — never a taxonomy.
const CHAN_COLORS = {
  google_ads: "#129ef5",
  paid_social: "#7c5cff",
  other_paid: "#ff8a3d",
  organic_search_direct: "#22b07d",
  organic_social: "#12b5c9",
  referrals_partner: "#e0568a",
  offline: "#8896a6",
  unclassified: "#b6bfca",
};
const CHAN_ORDER = [
  "google_ads", "paid_social", "other_paid", "organic_search_direct",
  "organic_social", "referrals_partner", "offline", "unclassified",
];
const CHAN_METRICS = [
  { key: "sqls", label: "SQLs" },
  { key: "customers", label: "Customers" },
  { key: "revenue", label: "Revenue" },
];

function chanColor(channel) {
  return CHAN_COLORS[channel] || "#b6bfca";
}

function chanOrderIndex(channel) {
  const i = CHAN_ORDER.indexOf(channel);
  return i === -1 ? CHAN_ORDER.length : i;
}

async function loadDashboardChannels() {
  const window_ = getRoasBusinessWindow();
  const token = ++_revReqSeq.dashChannels;
  setWindowRangeLoading("dashboard-range");

  const root = document.getElementById("dashboard-channels-root");
  const genEl = document.getElementById("dashboard-generated");
  if (root) {
    if (dashChannels && dashChannelsStatus === "ok") {
      root.classList.add("is-refreshing");
    } else {
      root.innerHTML = renderDashSkeleton();
    }
  }

  try {
    const data = await fetchJSON(
      `/api/dashboard/channels?window=${encodeURIComponent(window_)}`
    );
    if (token !== _revReqSeq.dashChannels) return;
    if (!_revResponseIsCurrent("dashChannels", token, data)) return;
    dashChannels = data;
    dashChannelsStatus = "ok";
    renderWindowRange("dashboard-range", data.window || null);
    if (genEl) genEl.textContent = data.generated_at ? `Generated ${fmtDate(data.generated_at)}` : "";
    renderDashboardChannels();
  } catch (err) {
    console.error("[loadDashboardChannels]", err);
    if (token !== _revReqSeq.dashChannels) return;
    dashChannels = null;
    dashChannelsStatus = "error";
    if (genEl) genEl.textContent = "";
    renderWindowRange("dashboard-range", null);
    renderDashboardChannels();
  }
}

function renderDashboardChannels() {
  const root = document.getElementById("dashboard-channels-root");
  if (!root) return;
  root.classList.remove("is-refreshing");

  if (dashChannelsStatus !== "ok" || !dashChannels) {
    const nextStep = dashCanNavigate("health")
      ? 'Next step: check <a href="#/health">System Status</a> or retry with Refresh.'
      : "Next step: retry with Refresh, or contact your administrator.";
    root.innerHTML = `
      <div class="revenue-blocked-card dash-error-card">
        <h3 class="revenue-blocked-card__title">Channels &amp; Platforms unavailable</h3>
        <p>The dashboard channels service could not be reached. Nothing is shown
        because nothing safe could be computed — metrics are never fabricated.</p>
        <p class="revenue-blocked-card__next">${nextStep}</p>
      </div>`;
    return;
  }

  const d = dashChannels;
  root.innerHTML = `
    <div class="dash-tab-intro dash-anim">
      <h2 class="dash-tab-intro__title">Channels &amp; Platforms</h2>
      <p class="dash-tab-intro__sub">Which channels and platforms produce SQLs, customers and closed-won revenue — and which are revenue-only because spend is not connected. Only Google Ads is spend-connected.</p>
    </div>
    ${renderChanKpiRow(d)}
    <div class="dash-main-grid">
      <section class="dash-panel dash-chart-panel dash-anim" aria-label="Channel momentum">
        ${renderChanMomentumPanel(d)}
      </section>
      <aside class="dash-panel dash-anim chan-mix-panel" aria-label="Channel mix">
        ${renderChanMixPanel(d)}
      </aside>
    </div>
    <section class="dash-panel dash-anim chan-quality-panel" aria-label="Channel quality vs revenue">
      ${renderChanQualityPanel(d)}
    </section>
    <section class="dash-panel dash-anim chan-platform-panel" aria-label="Platform matrix">
      ${renderChanPlatformMatrix(d)}
    </section>
    <section class="dash-panel dash-anim chan-channels-panel" aria-label="Channel detail">
      ${renderChanChannelList(d)}
    </section>
    <section class="dash-decisions dash-anim" aria-label="Channel decision cards">
      ${renderDashDecisionCards(d)}
    </section>
    ${renderChanTruthFooter(d)}
  `;
  wireChanMomentumToggle(root, d);
  wireChanMomentumHover(root, d);
  wireChanQualityHover(root, d);
  wireChanChannelDrawers(root, d);
}

function renderChanKpiRow(d) {
  const k = d.kpis || {};
  const pc = d.period_change || {};
  const shareSub = (k.spend_connected_share !== null && k.spend_connected_share !== undefined)
    ? `${(k.spend_connected_share * 100).toFixed(0)}% of closed-won revenue`
    : "Share unavailable";

  const cards = [
    {
      key: "revenue", label: "Closed-Won Revenue",
      value: dashValue(k.closed_won_revenue_usd, fmtMoney),
      sub: "USD · HubSpot closed-won",
      delta: dashDeltaChip("closed_won_revenue_usd", pc),
      source: "HubSpot Closed-Won",
      ok: k.closed_won_revenue_usd !== null && k.closed_won_revenue_usd !== undefined,
    },
    {
      key: "spend_connected", label: "Spend-Connected Revenue",
      value: dashValue(k.spend_connected_revenue_usd, fmtMoney),
      sub: shareSub,
      delta: `<span class="dash-delta dash-delta--none">Google Ads only</span>`,
      source: "Google Ads · spend-connected",
      ok: k.spend_connected_revenue_usd !== null && k.spend_connected_revenue_usd !== undefined,
    },
    {
      key: "revenue_only", label: "Revenue-Only Revenue",
      value: dashValue(k.revenue_only_revenue_usd, fmtMoney),
      sub: "No connected spend source",
      delta: `<span class="dash-delta dash-delta--none">Attribution only</span>`,
      source: "HubSpot Closed-Won",
      ok: k.revenue_only_revenue_usd !== null && k.revenue_only_revenue_usd !== undefined,
    },
    {
      key: "customers", label: "Customers",
      value: dashValue(k.total_customers, fmtCount),
      sub: k.top_channel ? `Top: ${escapeHtml(k.top_channel)}` : "Closed-won deals",
      delta: dashDeltaChip("total_customers", pc),
      source: "HubSpot Closed-Won",
      ok: k.total_customers !== null && k.total_customers !== undefined,
    },
    {
      key: "sqls", label: "SQLs",
      value: dashValue(k.total_sqls, fmtCount),
      sub: k.top_platform ? `Top platform: ${escapeHtml(k.top_platform)}` : "Qualified leads",
      delta: dashDeltaChip("total_sqls", pc),
      source: "HubSpot qualified leads",
      ok: k.total_sqls !== null && k.total_sqls !== undefined,
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

// Value of one trend channel for the selected metric. Revenue may be null
// (unknown amount) — never coerced to 0; counts (sqls/customers) are always real.
function chanMetricVal(ch, metric) {
  if (metric === "revenue") return ch.revenue_usd;
  return ch[metric];
}

function renderChanMomentumPanel(d) {
  const trend = d.trend || {};
  const metric = _chanMomentumMetric;
  const bucketLabel = trend.bucket === "month" ? "Monthly" : (trend.bucket === "week" ? "Weekly" : "");
  const note = trend.status !== "ready"
    ? `<p class="dash-series-note">Channel series unavailable — ${escapeHtml(trend.reason || "not verified")}</p>` : "";
  const toggle = CHAN_METRICS.map((m) => `
    <button type="button" class="chan-metric-btn ${m.key === metric ? "is-active" : ""}" data-chan-metric="${m.key}" aria-pressed="${m.key === metric}">${escapeHtml(m.label)}</button>`).join("");
  // Legend of the channels present in the series (order-stable).
  const present = new Set();
  (trend.points || []).forEach((p) => (p.channels || []).forEach((ch) => present.add(ch.channel)));
  const legend = CHAN_ORDER.filter((c) => present.has(c)).map((c) => {
    const label = ((d.channel_mix || []).find((r) => r.channel === c) || {}).channel_label || c;
    return `<span class="dash-legend__item"><span class="dash-legend__swatch" style="background:${chanColor(c)}"></span>${escapeHtml(label)}</span>`;
  }).join("");

  return `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Channel Momentum</h3>
        <p class="dash-panel__sub">Which channels are growing?${bucketLabel ? ` · ${bucketLabel}` : ""}</p>
      </div>
      <div class="chan-metric-toggle" role="group" aria-label="Momentum metric">${toggle}</div>
    </div>
    ${note}
    ${chanMomentumChartSVG(trend, metric)}
    <div class="dash-legend chan-legend">${legend}</div>`;
}

// Stacked channel bars per period for the selected metric. A channel whose
// revenue is unknown (null) is omitted from a revenue stack — never drawn as $0;
// the tooltip still names it Unavailable so real activity is never hidden.
function chanMomentumChartSVG(trend, metric) {
  const points = (trend && trend.points) || [];
  if (!trend || trend.status !== "ready" || !points.length) {
    return '<div class="dash-chart-empty">No chartable channel activity in this window.</div>';
  }
  const W = 760, H = 250, padL = 52, padR = 14, padT = 18, padB = 34;
  const plotW = W - padL - padR, plotH = H - padT - padB, n = points.length;
  const slot = plotW / n, barW = Math.min(30, slot * 0.6);
  const stackTotal = (p) => (p.channels || []).reduce((s, ch) => {
    const v = chanMetricVal(ch, metric);
    return s + (v === null || v === undefined || v <= 0 ? 0 : v);
  }, 0);
  const totals = points.map(stackTotal);
  const yMax = dashNiceCeil(Math.max(1, ...totals));
  const xCenter = (i) => padL + slot * i + slot / 2;
  const baseline = padT + plotH;
  const hOf = (v) => plotH * (v / yMax);

  const ticks = [0.25, 0.5, 0.75, 1];
  const grid = ticks.map((t) => {
    const y = (padT + plotH * (1 - t)).toFixed(1);
    const lbl = metric === "revenue" ? dashCompactMoney(yMax * t) : fmtCount(Math.round(yMax * t));
    return `<line x1="${padL}" x2="${W - padR}" y1="${y}" y2="${y}" stroke="#e0e6ef" stroke-width="1"/>
      <text x="${padL - 8}" y="${Number(y) + 3}" text-anchor="end" class="dash-chart-tick">${escapeHtml(lbl)}</text>`;
  }).join("");

  const bars = points.map((p, i) => {
    let yTop = baseline;
    const segs = (p.channels || []).slice().sort((a, b) => chanOrderIndex(b.channel) - chanOrderIndex(a.channel)).map((ch) => {
      const v = chanMetricVal(ch, metric);
      if (v === null || v === undefined || v <= 0) return "";
      const h = hOf(v);
      const x = xCenter(i) - barW / 2;
      const y = yTop - h;
      yTop = y;
      return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${h.toFixed(1)}" fill="${chanColor(ch.channel)}" fill-opacity="0.92"/>`;
    }).join("");
    return segs;
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
    `<rect class="dash-chart-hit" data-chan-idx="${i}" x="${(padL + slot * i).toFixed(1)}" y="${padT}"
      width="${slot.toFixed(1)}" height="${plotH}" fill="transparent"/>`).join("");

  return `
    <div class="dash-chart-wrap chan-momentum-wrap">
      <svg viewBox="0 0 ${W} ${H}" class="dash-combo-chart" role="img"
        aria-label="Channel ${escapeHtml(metric)} per ${escapeHtml(trend.bucket || "period")}, stacked by channel">
        ${grid}
        <line x1="${padL}" x2="${W - padR}" y1="${baseline}" y2="${baseline}" stroke="#cbd5e1" stroke-width="1"/>
        ${bars}
        ${xLabels}
        ${hitBands}
      </svg>
      <div class="dash-chart-tooltip" hidden></div>
    </div>`;
}

function wireChanMomentumToggle(root, d) {
  root.querySelectorAll(".chan-metric-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const m = btn.dataset.chanMetric;
      if (!m || m === _chanMomentumMetric) return;
      _chanMomentumMetric = m;
      renderDashboardChannels();
    });
  });
}

function chanMetricDisplay(ch, metric) {
  const v = chanMetricVal(ch, metric);
  if (v === null || v === undefined) return "Unavailable";
  return metric === "revenue" ? fmtMoney(v) : String(v);
}

function wireChanMomentumHover(root, d) {
  const wrap = root.querySelector(".chan-momentum-wrap");
  if (!wrap) return;
  const tooltip = wrap.querySelector(".dash-chart-tooltip");
  const points = ((d.trend || {}).points) || [];
  const metric = _chanMomentumMetric;

  function hide() {
    if (tooltip) tooltip.hidden = true;
    wrap.querySelectorAll(".dash-chart-hit.is-hover").forEach((el) => el.classList.remove("is-hover"));
  }
  wrap.addEventListener("pointermove", (e) => {
    const band = e.target.closest(".dash-chart-hit");
    if (!band || !tooltip) { hide(); return; }
    const p = points[Number(band.dataset.chanIdx)];
    if (!p) { hide(); return; }
    wrap.querySelectorAll(".dash-chart-hit.is-hover").forEach((el) => el.classList.remove("is-hover"));
    band.classList.add("is-hover");
    tooltip.textContent = "";
    const title = document.createElement("div");
    title.className = "dash-chart-tooltip__title";
    title.textContent = p.period_label || "";
    tooltip.appendChild(title);
    const chans = (p.channels || []).slice().sort((a, b) => chanOrderIndex(a.channel) - chanOrderIndex(b.channel));
    if (!chans.length) {
      const row = document.createElement("div");
      row.className = "dash-chart-tooltip__row";
      row.textContent = "No channel activity";
      tooltip.appendChild(row);
    }
    chans.forEach((ch) => {
      const row = document.createElement("div");
      row.className = "dash-chart-tooltip__row";
      const key = document.createElement("span");
      key.className = "dash-chart-tooltip__key";
      key.style.background = chanColor(ch.channel);
      row.appendChild(key);
      const strong = document.createElement("strong");
      strong.textContent = chanMetricDisplay(ch, metric);
      const name = document.createElement("span");
      name.textContent = ` ${ch.channel_label || ch.channel}`;
      row.appendChild(strong);
      row.appendChild(name);
      tooltip.appendChild(row);
    });
    const wrapRect = wrap.getBoundingClientRect();
    const bandRect = band.getBoundingClientRect();
    tooltip.hidden = false;
    const tipW = tooltip.offsetWidth || 160;
    let left = bandRect.left - wrapRect.left + bandRect.width / 2 - tipW / 2;
    left = Math.max(4, Math.min(left, wrapRect.width - tipW - 4));
    tooltip.style.left = `${left}px`;
    tooltip.style.top = "8px";
  });
  wrap.addEventListener("pointerleave", hide);
}

function renderChanMixPanel(d) {
  const rows = (d.channel_mix || []).slice().sort((a, b) => (b.customers || 0) - (a.customers || 0));
  const legend = rows.map((r) => `
    <li class="chan-mix-row">
      <span class="chan-mix-row__dot" style="background:${chanColor(r.channel)}"></span>
      <span class="chan-mix-row__name" title="${escapeHtml(r.channel_label || "")}">${escapeHtml(r.channel_label || "—")}</span>
      <span class="chan-mix-row__cust">${dashValue(r.customers, fmtCount)} cust</span>
      <span class="chan-mix-row__rev">${dashValue(r.won_revenue_usd, fmtMoney)}</span>
    </li>`).join("");
  return `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Channel Mix</h3>
        <p class="dash-panel__sub">Customer share by channel</p>
      </div>
    </div>
    <div class="chan-mix">
      <div class="chan-donut-wrap">${chanMixDonutSVG(rows)}</div>
      <ul class="chan-mix-list">${legend || '<li class="rev-breakdown-empty">No channels in this window.</li>'}</ul>
    </div>`;
}

function chanMixDonutSVG(rows) {
  const data = (rows || [])
    .map((r) => ({ label: r.channel_label, channel: r.channel, value: r.customers || 0 }))
    .filter((r) => r.value > 0);
  const total = data.reduce((s, r) => s + r.value, 0);
  if (!total) return '<div class="dash-chart-empty">No customers to attribute in this window.</div>';
  const cx = 90, cy = 90, rOuter = 78, rInner = 48;
  let a0 = -Math.PI / 2;
  const arcs = data.map((r) => {
    const frac = r.value / total;
    // A single full-circle slice can't be drawn as one arc; nudge it just short.
    const a1 = a0 + Math.min(frac, 0.9999) * 2 * Math.PI;
    const large = (a1 - a0) > Math.PI ? 1 : 0;
    const x0 = cx + rOuter * Math.cos(a0), y0 = cy + rOuter * Math.sin(a0);
    const x1 = cx + rOuter * Math.cos(a1), y1 = cy + rOuter * Math.sin(a1);
    const xi1 = cx + rInner * Math.cos(a1), yi1 = cy + rInner * Math.sin(a1);
    const xi0 = cx + rInner * Math.cos(a0), yi0 = cy + rInner * Math.sin(a0);
    const path = `M${x0.toFixed(1)},${y0.toFixed(1)} A${rOuter},${rOuter} 0 ${large} 1 ${x1.toFixed(1)},${y1.toFixed(1)} L${xi1.toFixed(1)},${yi1.toFixed(1)} A${rInner},${rInner} 0 ${large} 0 ${xi0.toFixed(1)},${yi0.toFixed(1)} Z`;
    a0 = a1;
    return `<path d="${path}" fill="${chanColor(r.channel)}" fill-opacity="0.92"><title>${escapeHtml(r.label || r.channel)}: ${escapeHtml(fmtCount(r.value))} customers (${Math.round(frac * 100)}%)</title></path>`;
  }).join("");
  return `
    <svg viewBox="0 0 180 180" class="chan-donut" role="img" aria-label="Customer mix by channel">
      ${arcs}
      <text x="90" y="86" text-anchor="middle" class="chan-donut__total">${escapeHtml(fmtCount(total))}</text>
      <text x="90" y="103" text-anchor="middle" class="chan-donut__label">customers</text>
    </svg>`;
}

function renderChanQualityPanel(d) {
  const pts = d.quality_matrix || [];
  return `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Quality vs Revenue</h3>
        <p class="dash-panel__sub">SQLs (demand) × Customers (conversion) · bubble size = closed-won revenue</p>
      </div>
    </div>
    ${chanQualityBubbleSVG(pts)}`;
}

function chanQualityBubbleSVG(points) {
  const pts = (points || []);
  if (!pts.length) return '<div class="dash-chart-empty">No channel quality data in this window.</div>';
  const W = 760, H = 300, padL = 56, padR = 22, padT = 20, padB = 42;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  // Headroom (×1.15) so the largest bubble at the max-x/max-y corner sits inside
  // the plot instead of clipping against the frame.
  const maxX = dashNiceCeil(Math.max(1, ...pts.map((p) => p.sqls || 0)) * 1.15);
  const maxY = dashNiceCeil(Math.max(1, ...pts.map((p) => p.customers || 0)) * 1.15);
  const maxRev = Math.max(1, ...pts.map((p) => p.revenue_usd || 0));
  const xOf = (v) => padL + plotW * ((v || 0) / maxX);
  const yOf = (v) => padT + plotH * (1 - (v || 0) / maxY);
  // Null revenue → smallest neutral bubble (never sized as if $0 were a value).
  const rOf = (rev) => (rev === null || rev === undefined) ? 6 : 9 + 24 * Math.sqrt(rev / maxRev);

  const ticks = [0, 0.25, 0.5, 0.75, 1];
  const gridY = ticks.map((t) => {
    const y = (padT + plotH * (1 - t)).toFixed(1);
    return `<line x1="${padL}" x2="${W - padR}" y1="${y}" y2="${y}" stroke="#eef1f6" stroke-width="1"/>
      <text x="${padL - 8}" y="${Number(y) + 3}" text-anchor="end" class="dash-chart-tick">${escapeHtml(fmtCount(Math.round(maxY * t)))}</text>`;
  }).join("");
  const gridX = ticks.map((t) => {
    const x = xOf(maxX * t).toFixed(1);
    return `<text x="${x}" y="${H - 10}" text-anchor="middle" class="dash-chart-tick">${escapeHtml(fmtCount(Math.round(maxX * t)))}</text>`;
  }).join("");

  const bubbles = pts.slice().sort((a, b) => rOf(b.revenue_usd) - rOf(a.revenue_usd)).map((p) => {
    const i = pts.indexOf(p);
    const r = rOf(p.revenue_usd);
    const cx = xOf(p.sqls).toFixed(1), cy = yOf(p.customers).toFixed(1);
    const stroke = p.spend_connected ? "#0a5f92" : "#ffffff";
    return `<circle class="dash-chart-hit chan-bubble" data-chan-bubble="${i}" cx="${cx}" cy="${cy}" r="${r.toFixed(1)}"
      fill="${chanColor(p.channel)}" fill-opacity="0.55" stroke="${stroke}" stroke-width="1.6"/>`;
  }).join("");

  return `
    <div class="dash-chart-wrap chan-bubble-wrap">
      <svg viewBox="0 0 ${W} ${H}" class="dash-combo-chart" role="img"
        aria-label="Channel quality: SQLs on x, customers on y, revenue as bubble size">
        ${gridY}
        <line x1="${padL}" x2="${W - padR}" y1="${(padT + plotH).toFixed(1)}" y2="${(padT + plotH).toFixed(1)}" stroke="#cbd5e1" stroke-width="1"/>
        <line x1="${padL}" x2="${padL}" y1="${padT}" y2="${(padT + plotH).toFixed(1)}" stroke="#cbd5e1" stroke-width="1"/>
        ${gridX}
        <text x="${(padL + plotW / 2).toFixed(1)}" y="${H - 26}" text-anchor="middle" class="dash-chart-axis">SQLs →</text>
        ${bubbles}
      </svg>
      <div class="dash-chart-tooltip" hidden></div>
    </div>`;
}

function wireChanQualityHover(root, d) {
  const wrap = root.querySelector(".chan-bubble-wrap");
  if (!wrap) return;
  const tooltip = wrap.querySelector(".dash-chart-tooltip");
  const pts = d.quality_matrix || [];

  function hide() {
    if (tooltip) tooltip.hidden = true;
    wrap.querySelectorAll(".chan-bubble.is-hover").forEach((el) => el.classList.remove("is-hover"));
  }
  wrap.addEventListener("pointermove", (e) => {
    const dot = e.target.closest(".chan-bubble");
    if (!dot || !tooltip) { hide(); return; }
    const p = pts[Number(dot.dataset.chanBubble)];
    if (!p) { hide(); return; }
    wrap.querySelectorAll(".chan-bubble.is-hover").forEach((el) => el.classList.remove("is-hover"));
    dot.classList.add("is-hover");
    tooltip.textContent = "";
    const title = document.createElement("div");
    title.className = "dash-chart-tooltip__title";
    title.textContent = p.channel_label || p.channel || "";
    tooltip.appendChild(title);
    [["SQLs", p.sqls === null || p.sqls === undefined ? "Unavailable" : String(p.sqls)],
     ["Customers", p.customers === null || p.customers === undefined ? "Unavailable" : String(p.customers)],
     ["Revenue", p.revenue_usd === null || p.revenue_usd === undefined ? "Unavailable" : fmtMoney(p.revenue_usd)],
     ["Spend", p.spend_connected ? "Connected (Google Ads)" : "Not connected"]].forEach(([label, val]) => {
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
    const tipW = tooltip.offsetWidth || 160;
    let left = dotRect.left - wrapRect.left + dotRect.width / 2 - tipW / 2;
    left = Math.max(4, Math.min(left, wrapRect.width - tipW - 4));
    tooltip.style.left = `${left}px`;
    tooltip.style.top = "8px";
  });
  wrap.addEventListener("pointerleave", hide);
}

// Platform matrix. Only Google Ads carries spend / ROAS; every other platform
// renders "Unavailable — no connected spend source" and NEVER a ROAS number.
const CHAN_SPEND_UNAVAILABLE = "Unavailable — no connected spend source";

function chanPlatformSpendCell(p) {
  // Show USD spend whenever present (Google Ads, FX-verified). Spend is
  // independent of ROAS, which is gated separately by roas_available.
  if (p.spend_usd !== null && p.spend_usd !== undefined) {
    return escapeHtml(fmtMoney(p.spend_usd));
  }
  // FX/USD unavailable but the Google Ads spend source is connected: show native
  // GBP (never $) so the row is not mislabelled "no connected spend source".
  const nat = p.native_spend;
  if (nat && nat.amount !== null && nat.amount !== undefined) {
    return escapeHtml(fmtCompactCurrency(nat.amount, nat.currency || "GBP"));
  }
  return `<span class="dash-unavailable" title="${escapeHtml(CHAN_SPEND_UNAVAILABLE)}">${escapeHtml(CHAN_SPEND_UNAVAILABLE)}</span>`;
}

function chanPlatformRoasCell(p) {
  // ROAS is shown ONLY when the platform is spend-connected (Google Ads) with a
  // real ROAS. Non-connected platforms never show a multiple — not even $0/0.00x.
  if (p.roas_available && p.roas !== null && p.roas !== undefined) {
    return escapeHtml(fmtRoasMultiple(p.roas));
  }
  return `<span class="dash-unavailable">Unavailable</span>`;
}

function renderChanPlatformMatrix(d) {
  const rows = d.platform_matrix || [];
  if (!rows.length) {
    return `
      <div class="dash-panel__header">
        <div><h3 class="dash-panel__title">Platform Matrix</h3></div>
      </div>
      <p class="rev-breakdown-empty">No platform activity in this window.</p>`;
  }
  const head = ["Platform", "Channel", "Leads", "SQLs", "Customers", "Revenue", "Spend", "ROAS", "Status"]
    .map((h) => `<th>${escapeHtml(h)}</th>`).join("");
  const body = rows.map((p) => `
    <tr class="chan-platform-row">
      <td class="chan-platform-td chan-platform-td--name">
        <span class="chan-mix-row__dot" style="background:${chanColor(p.channel)}"></span>${escapeHtml(p.platform_label || "—")}
      </td>
      <td>${escapeHtml(p.channel_label || "—")}</td>
      <td class="chan-num">${dashValue(p.leads, fmtCount)}</td>
      <td class="chan-num">${dashValue(p.sqls, fmtCount)}</td>
      <td class="chan-num">${dashValue(p.customers, fmtCount)}</td>
      <td class="chan-num">${dashValue(p.won_revenue_usd, fmtMoney)}</td>
      <td class="chan-num">${chanPlatformSpendCell(p)}</td>
      <td class="chan-num">${chanPlatformRoasCell(p)}</td>
      <td><span class="chan-status ${p.spend_connected ? "chan-status--connected" : "chan-status--revenue"}">${escapeHtml(p.status || "")}</span></td>
    </tr>`).join("");
  return `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Platform Matrix</h3>
        <p class="dash-panel__sub">Every platform's SQLs, customers and revenue · spend &amp; ROAS only where a spend source is connected</p>
      </div>
    </div>
    <div class="chan-platform-scroll">
      <table class="chan-platform-table">
        <thead><tr>${head}</tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>`;
}

function renderChanChannelList(d) {
  const rows = d.channel_mix || [];
  if (!rows.length) {
    return `
      <div class="dash-panel__header">
        <div><h3 class="dash-panel__title">Channels — Detail</h3></div>
      </div>
      <p class="rev-breakdown-empty">No channel activity in this window.</p>`;
  }
  const body = rows.map((r, i) => {
    // Show USD spend when present (append ROAS only when it is a real, trusted
    // value — never a "· Unavailable" suffix, never on a non-Google row); else
    // fall back to native GBP (spend source connected, FX/USD unavailable); else
    // Unavailable. Mirrors chanPlatformSpendCell / chanPlatformRoasCell gating.
    const spendKnown = r.spend_usd !== null && r.spend_usd !== undefined;
    const roasKnown = r.roas_available && r.roas !== null && r.roas !== undefined;
    const nat = r.native_spend;
    const nativeKnown = nat && nat.amount !== null && nat.amount !== undefined;
    let spendCell;
    if (spendKnown) {
      spendCell = `${escapeHtml(fmtMoney(r.spend_usd))}${roasKnown ? ` · ${escapeHtml(fmtRoasMultiple(r.roas))}` : ""}`;
    } else if (nativeKnown) {
      spendCell = escapeHtml(fmtCompactCurrency(nat.amount, nat.currency || "GBP"));
    } else {
      spendCell = `<span class="dash-unavailable">${escapeHtml(CHAN_SPEND_UNAVAILABLE)}</span>`;
    }
    return `
    <li class="chan-channel-row" data-chan-row="${i}" tabindex="0" role="button" aria-label="Open ${escapeHtml(r.channel_label || "channel")} detail">
      <span class="chan-channel-row__dot" style="background:${chanColor(r.channel)}"></span>
      <span class="chan-channel-row__name">${escapeHtml(r.channel_label || "—")}</span>
      <span class="chan-channel-row__metric">${dashValue(r.sqls, fmtCount)} SQLs</span>
      <span class="chan-channel-row__metric">${dashValue(r.customers, fmtCount)} cust</span>
      <span class="chan-channel-row__metric">${dashValue(r.won_revenue_usd, fmtMoney)}</span>
      <span class="chan-channel-row__spend">${spendCell}</span>
      <span class="chan-status ${r.spend_connected ? "chan-status--connected" : "chan-status--revenue"}">${escapeHtml(r.status || "")}</span>
    </li>`;
  }).join("");
  return `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Channels — Detail</h3>
        <p class="dash-panel__sub">Click a channel for its platform breakdown</p>
      </div>
    </div>
    <ul class="chan-channel-list">${body}</ul>`;
}

function wireChanChannelDrawers(root, d) {
  const rows = d.channel_mix || [];
  const platforms = d.platform_matrix || [];
  const open = (i) => {
    const r = rows[i];
    if (!r) return;
    const own = platforms.filter((p) => p.channel === r.channel);
    const pfBody = own.length
      ? own.map((p) => `
          <tr>
            <td>${escapeHtml(p.platform_label || "—")}</td>
            <td class="chan-num">${dashValue(p.sqls, fmtCount)}</td>
            <td class="chan-num">${dashValue(p.customers, fmtCount)}</td>
            <td class="chan-num">${dashValue(p.won_revenue_usd, fmtMoney)}</td>
            <td class="chan-num">${chanPlatformSpendCell(p)}</td>
            <td class="chan-num">${chanPlatformRoasCell(p)}</td>
          </tr>`).join("")
      : `<tr><td colspan="6" class="rev-breakdown-empty">No platform rows for this channel.</td></tr>`;
    let drawer = root.querySelector(".chan-channel-drawer");
    if (!drawer) {
      drawer = document.createElement("div");
      drawer.className = "chan-channel-drawer";
      root.querySelector(".chan-channels-panel").appendChild(drawer);
    }
    drawer.innerHTML = `
      <div class="chan-channel-drawer__head">
        <strong>${escapeHtml(r.channel_label || "Channel")} — platform breakdown</strong>
        <span class="chan-status ${r.spend_connected ? "chan-status--connected" : "chan-status--revenue"}">${escapeHtml(r.status || "")}</span>
        <button type="button" class="chan-channel-drawer__close" aria-label="Close">×</button>
      </div>
      <p class="chan-channel-drawer__note">Platform-level SQLs, customers and revenue for this channel — not individual client/deal records.</p>
      <table class="chan-drawer-table">
        <thead><tr><th>Platform</th><th>SQLs</th><th>Customers</th><th>Revenue</th><th>Spend</th><th>ROAS</th></tr></thead>
        <tbody>${pfBody}</tbody>
      </table>`;
    drawer.querySelector(".chan-channel-drawer__close").addEventListener("click", () => drawer.remove());
    drawer.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  root.querySelectorAll(".chan-channel-row").forEach((li) => {
    li.addEventListener("click", () => open(Number(li.dataset.chanRow)));
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(Number(li.dataset.chanRow)); }
    });
  });
}

function renderChanTruthFooter(d) {
  const ts = d.truth_status || {};
  const chip = (label, status) => `
    <span class="dash-footer-chip" title="${escapeHtml(`${label}: ${status || "unknown"}`)}">
      <span class="dash-dot ${DASH_STATUS_DOT[status] || "dash-dot--muted"}"></span>${escapeHtml(label)}
    </span>`;
  const allGood = ts.revenue === "ready" && ts.source_attribution === "ready"
    && ts.google_ads_spend === "ready" && ts.platform_classification === "ready";
  return `
    <footer class="dash-truth-footer ${allGood ? "dash-truth-footer--ok" : ""}">
      ${chip("Revenue", ts.revenue)}
      ${chip("Source attr.", ts.source_attribution)}
      ${chip("Google Ads spend", ts.google_ads_spend)}
      ${chip("Non-Google spend", ts.non_google_spend)}
      ${chip("Platform class.", ts.platform_classification)}
      <span class="dash-footer-chip dash-footer-chip--readonly">Read-only</span>
      ${allGood
        ? '<span class="dash-footer-note">Channel truth verified — only Google Ads is spend-connected; other channels are revenue-only by design.</span>'
        : (dashCanNavigate("revenue-health")
          ? '<a class="dash-footer-note" href="#/revenue-health">Some attribution needs review — open Revenue Health</a>'
          : '<span class="dash-footer-note">Some attribution needs review — contact your administrator.</span>')}
    </footer>`;
}

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
        <h3 class="revenue-blocked-card__title">Campaigns &amp; Keywords unavailable</h3>
        <p>The dashboard campaigns service could not be reached. Nothing is shown
        because nothing safe could be computed — metrics are never fabricated.</p>
        <p class="revenue-blocked-card__next">${nextStep}</p>
      </div>`;
    return;
  }

  const d = dashCampaigns;
  root.innerHTML = `
    <div class="dash-tab-intro dash-anim">
      <h2 class="dash-tab-intro__title">Campaigns &amp; Keywords</h2>
      <p class="dash-tab-intro__sub">Which Google Ads campaigns and keyword themes produce SQLs, customers and closed-won revenue — and which spend without proof. Spend is Google Ads (native GBP); revenue is HubSpot closed-won (USD).</p>
    </div>
    ${renderCampKpiRow(d)}
    <section class="dash-panel dash-chart-panel dash-anim camp-map-panel" aria-label="Campaign performance map">
      ${renderCampPerformanceMap(d)}
    </section>
    <section class="dash-panel dash-anim camp-leaderboard-panel" aria-label="Campaign leaderboard">
      ${renderCampLeaderboard(d)}
    </section>
    <div class="dash-main-grid">
      <section class="dash-panel dash-anim camp-keyword-panel" aria-label="Keyword theme intelligence">
        ${renderCampKeywordThemes(d)}
      </section>
      <aside class="dash-panel dash-anim camp-search-panel" aria-label="Search term signals">
        ${renderCampSearchSignals(d)}
      </aside>
    </div>
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
    return '<span class="dash-unavailable">Unavailable</span>';
  }
  const native = escapeHtml(fmtCompactCurrency(row.spend_native, row.native_currency || "GBP"));
  if (row.spend_usd !== null && row.spend_usd !== undefined) {
    return `${native} <span class="camp-usd">· ${escapeHtml(fmtMoney(row.spend_usd))}</span>`;
  }
  return native;
}

function campRoasCell(row) {
  // ROAS only when it is a real, trusted value; else the status conveys the outcome.
  if (row.roas_available && row.roas !== null && row.roas !== undefined) {
    return `<span class="camp-roas">${escapeHtml(fmtRoasMultiple(row.roas))}</span>`;
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
      key: "sqls", label: "SQLs from Google Ads",
      value: dashValue(k.sqls, fmtCount), sub: "Qualified leads · this window",
      delta: dashDeltaChip("sqls", pc),
      source: "HubSpot qualified leads",
      ok: k.sqls !== null && k.sqls !== undefined,
    },
    {
      key: "customers", label: "Customers",
      value: dashValue(k.customers, fmtCount), sub: "Closed-won · campaign-attributed",
      delta: dashDeltaChip("customers", pc),
      source: "HubSpot Closed-Won",
      ok: k.customers !== null && k.customers !== undefined,
    },
    {
      key: "revenue", label: "Won Revenue",
      value: dashValue(k.won_revenue_usd, fmtMoney), sub: "USD · HubSpot closed-won",
      delta: dashDeltaChip("won_revenue_usd", pc),
      source: "HubSpot Closed-Won",
      ok: k.won_revenue_usd !== null && k.won_revenue_usd !== undefined,
    },
    {
      key: "roas", label: "Google Ads ROAS",
      value: (k.roas !== null && k.roas !== undefined)
        ? escapeHtml(fmtRoasMultiple(k.roas))
        : '<span class="dash-unavailable">Unavailable</span>',
      // Plain-text sub (the card template escapes it) — never embed dashValue HTML.
      sub: `${(k.campaigns_with_spend === null || k.campaigns_with_spend === undefined) ? "—" : fmtCount(k.campaigns_with_spend)} campaigns with spend`,
      delta: dashDeltaChip("roas", pc),
      source: "Revenue ÷ verified USD spend",
      ok: k.roas !== null && k.roas !== undefined,
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
function renderCampPerformanceMap(d) {
  const rows = (d.campaigns || []).filter((r) =>
    r.spend_native !== null && r.spend_native !== undefined && r.spend_native > 0);
  const legend = Object.keys(CAMP_STATUS_META).map((s) =>
    `<span class="dash-legend__item"><span class="dash-legend__swatch" style="background:${CAMP_STATUS_META[s].color}"></span>${escapeHtml(s)}</span>`).join("");
  return `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Campaign Performance Map</h3>
        <p class="dash-panel__sub">Spend (native GBP) × Customers · bubble size = SQLs · colour = status</p>
      </div>
    </div>
    ${campBubbleSVG(rows)}
    <div class="dash-legend camp-legend">${legend}</div>`;
}

function campBubbleSVG(rows) {
  if (!rows.length) {
    return '<div class="dash-chart-empty">No campaigns with verified spend in this window.</div>';
  }
  const W = 760, H = 320, padL = 60, padR = 24, padT = 20, padB = 44;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const maxX = dashNiceCeil(Math.max(1, ...rows.map((r) => r.spend_native || 0)) * 1.15);
  const maxY = dashNiceCeil(Math.max(1, ...rows.map((r) => r.customers || 0)) * 1.15);
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
      const cx = xOf(r.spend_native).toFixed(1), cy = yOf(r.customers).toFixed(1);
      return `<circle class="dash-chart-hit camp-bubble" data-camp-bubble="${i}" cx="${cx}" cy="${cy}" r="${rad.toFixed(1)}"
      fill="${campStatusColor(r.status)}" fill-opacity="0.55" stroke="${campStatusColor(r.status)}" stroke-width="1.4"/>`;
    }).join("");

  return `
    <div class="dash-chart-wrap camp-bubble-wrap">
      <svg viewBox="0 0 ${W} ${H}" class="dash-combo-chart" role="img"
        aria-label="Campaign performance: native GBP spend on x, customers on y, SQLs as bubble size">
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
  const rows = (d.campaigns || []).filter((r) =>
    r.spend_native !== null && r.spend_native !== undefined && r.spend_native > 0);

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
    const roas = (r.roas_available && r.roas !== null && r.roas !== undefined) ? fmtRoasMultiple(r.roas) : "Unavailable";
    [["Spend (£)", r.spend_native !== null && r.spend_native !== undefined ? fmtCompactCurrency(r.spend_native, r.native_currency || "GBP") : "Unavailable"],
     ["Spend (USD)", usd],
     ["SQLs", r.sqls === null || r.sqls === undefined ? "Unavailable" : String(r.sqls)],
     ["Customers", r.customers === null || r.customers === undefined ? "Unavailable" : String(r.customers)],
     ["Revenue", r.won_revenue_usd === null || r.won_revenue_usd === undefined ? "Unavailable" : fmtMoney(r.won_revenue_usd)],
     ["ROAS", roas],
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
  "Revenue proven": "Scale — open ROAS by Campaign",
  "SQL producer": "Review SQL quality",
  "Spend without SQL / customer proof": "Review — spend without proof",
  "Revenue attribution missing": "Open Revenue Health",
  "FX unavailable": "FX incomplete — USD/ROAS withheld",
  "Mapping needs review": "Map campaign spend",
};

function renderCampLeaderboard(d) {
  const rows = d.campaigns || [];
  if (!rows.length) {
    return `
      <div class="dash-panel__header"><div><h3 class="dash-panel__title">Campaign Leaderboard</h3></div></div>
      <p class="rev-breakdown-empty">No campaign activity in this window.</p>`;
  }
  const head = ["Campaign", "Spend", "SQLs", "Customers", "Revenue", "ROAS / Status", "Next action"]
    .map((h) => `<th>${escapeHtml(h)}</th>`).join("");
  const body = rows.map((r, i) => {
    const isUnattributed = r.attribution_status === "unattributed";
    return `
    <tr class="camp-row ${isUnattributed ? "camp-row--unattributed" : ""}" data-camp-row="${i}" tabindex="0" role="button" aria-label="Open ${escapeHtml(r.campaign_name || "campaign")} detail">
      <td class="camp-td camp-td--name">
        <span class="camp-dot ${(CAMP_STATUS_META[r.status] || {}).dot || "camp-dot--muted"}"></span>${escapeHtml(r.campaign_name || "—")}
      </td>
      <td class="camp-num">${campSpendCell(r)}</td>
      <td class="camp-num">${dashValue(r.sqls, fmtCount)}</td>
      <td class="camp-num">${dashValue(r.customers, fmtCount)}</td>
      <td class="camp-num">${dashValue(r.won_revenue_usd, fmtMoney)}</td>
      <td>${campRoasCell(r)}</td>
      <td class="camp-next">${escapeHtml(CAMP_NEXT_ACTION[r.status] || "Open ROAS by Campaign")}</td>
    </tr>`;
  }).join("");
  return `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Campaign Leaderboard</h3>
        <p class="dash-panel__sub">Every campaign's spend, SQLs, customers and revenue · click a row for proof · ROAS only when spend + FX + revenue are safe</p>
      </div>
    </div>
    <div class="camp-scroll">
      <table class="camp-table">
        <thead><tr>${head}</tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>`;
}

function renderCampKeywordThemes(d) {
  const kt = d.keyword_themes || {};
  const header = `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Keyword Theme Intelligence</h3>
        <p class="dash-panel__sub">Recent Google Ads keyword spend by theme · read-only evidence</p>
      </div>
    </div>`;
  if (kt.status !== "ready" || !(kt.themes || []).length) {
    // Role-gate the Keywords link consistently with the tab's other navigation.
    const kwLink = dashCanNavigate("keywords")
      ? 'Open the <a href="#/keywords">Keywords</a> page for current keyword detail.'
      : "The Keywords page has current keyword detail.";
    return `${header}
      <p class="camp-keyword-note">Keyword themes are unavailable${kt.reason ? ` — ${escapeHtml(kt.reason)}` : ""}. ${kwLink}</p>`;
  }
  const note = `<p class="camp-keyword-note">Recent keyword snapshot${kt.run_date ? ` (as of ${escapeHtml(kt.run_date)})` : ""} — not window-scoped, and keyword-level SQLs / customers / revenue / ROAS have no durable attribution.</p>`;
  const rows = kt.themes.map((t) => `
    <li class="camp-theme-row">
      <span class="camp-theme-row__name">${escapeHtml(t.theme_label || "—")}</span>
      <span class="camp-theme-row__spend">${dashValue(t.spend_usd, fmtMoney)}</span>
      <span class="camp-theme-row__clicks">${dashValue(t.clicks, fmtCount)} clicks</span>
      <span class="camp-theme-row__kw">${dashValue(t.keywords, fmtCount)} kw</span>
      <span class="camp-theme-row__out">SQLs / revenue <span class="dash-unavailable">Unavailable</span></span>
    </li>`).join("");
  return `${header}${note}<ul class="camp-theme-list">${rows}</ul>`;
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
    ? '<a class="camp-signal-link" href="#/search-terms">Review in Search Terms →</a>' : "";
  const linkWaste = dashCanNavigate("waste")
    ? '<a class="camp-signal-link" href="#/waste">Open Flagged Waste Terms →</a>' : "";
  return `${header}
    <div class="camp-signal-grid">
      ${renderCampSignalList("Reviewed clean — not flagged waste", s.value_terms, "value")}
      ${renderCampSignalList("Waste — flagged", s.waste_terms, "waste")}
      ${renderCampSignalList("Needs review — unanalyzed", s.needs_review, "review")}
    </div>
    <div class="camp-signal-foot">${linkTerms}${linkWaste}<span class="camp-signal-note">Evidence only — no platform write.</span></div>`;
}

const CAMP_DEAL_COLUMNS = [
  ["company", "Company"], ["company_id", "Company ID"], ["main_contact", "Main Contact"],
  ["contact_id", "Contact ID"], ["deal", "Deal"], ["deal_id", "Deal ID"],
  ["amount_usd", "Amount"], ["close_date", "Close Date"], ["attribution_status", "Attribution"],
];

function campDealCell(deal, key) {
  if (key === "amount_usd") return dashValue(deal.amount_usd, fmtMoney); // null → Unavailable, never $0
  return dashValue(deal[key]);
}

function wireCampDrawers(root, d) {
  const rows = d.campaigns || [];
  const kt = d.keyword_themes || {};
  const open = (i) => {
    const r = rows[i];
    if (!r) return;
    const deals = r.deals || [];
    const dealProofAvailable = d.deal_proof_available !== false;
    const dealHead = CAMP_DEAL_COLUMNS.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("");
    // When the deal ledger is unavailable we cannot prove OR disprove deals — say
    // Unavailable, never "No closed-won deals" (which would imply the campaign has none).
    const dealBody = !dealProofAvailable
      ? `<tr><td colspan="${CAMP_DEAL_COLUMNS.length}" class="dash-unavailable">Closed-won deal proof unavailable — the deal ledger could not be read (revenue may still be canonical).</td></tr>`
      : deals.length
        ? deals.map((deal) => `<tr>${CAMP_DEAL_COLUMNS.map(([key]) => `<td class="camp-deal-td camp-deal-td--${key}">${campDealCell(deal, key)}</td>`).join("")}</tr>`).join("")
        : `<tr><td colspan="${CAMP_DEAL_COLUMNS.length}" class="rev-breakdown-empty">No closed-won deals to prove this campaign in this window.</td></tr>`;

    const links = [
      ["roas-campaigns", "Open ROAS by Campaign"],
      ["keywords", "Open Keywords"],
      ["search-terms", "Open Search Terms"],
      ["waste", "Open Flagged Waste Terms"],
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
        <div><dt>SQLs</dt><dd>${dashValue(r.sqls, fmtCount)}</dd></div>
        <div><dt>Customers</dt><dd>${dashValue(r.customers, fmtCount)}</dd></div>
        <div><dt>Revenue</dt><dd>${dashValue(r.won_revenue_usd, fmtMoney)}</dd></div>
        <div><dt>ROAS / status</dt><dd>${campRoasCell(r)}</dd></div>
        <div><dt>Mapping</dt><dd>${escapeHtml(r.attribution_status || "—")}</dd></div>
      </dl>
      <div class="camp-drawer__section">
        <h4 class="camp-drawer__h">Revenue proof — closed-won deals</h4>
        <div class="camp-deal-scroll">
          <table class="camp-deal-table"><thead><tr>${dealHead}</tr></thead><tbody>${dealBody}</tbody></table>
        </div>
      </div>
      <div class="camp-drawer__section">
        <h4 class="camp-drawer__h">Keyword themes</h4>
        <p class="camp-keyword-note">${kt.status === "ready"
          ? "Recent keyword themes are account-wide (not per-campaign) — open Keywords for campaign detail."
          : "Keyword themes are unavailable for this window."}</p>
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
      ${chip("Keyword attr.", ts.keyword_attribution)}
      <span class="dash-footer-chip dash-footer-chip--readonly">Read-only</span>
      ${allGood
        ? '<span class="dash-footer-note">Campaign truth verified — keyword/search-term panels are read-only evidence.</span>'
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
  "Customer proven": { color: "#3ac6a1", dot: "ctry-dot--customer" },
  "SQL producer": { color: "#129ef5", dot: "ctry-dot--sql" },
  "Spend without customer proof": { color: "#ff8a3d", dot: "ctry-dot--waste" },
  "FX unavailable": { color: "#8896a6", dot: "ctry-dot--muted" },
  "Geo attribution needs review": { color: "#7c5cff", dot: "ctry-dot--attr" },
  "No spend in window": { color: "#b6bfca", dot: "ctry-dot--muted" },
};
function ctryStatusColor(status) {
  return (CTRY_STATUS_META[status] || { color: "#b6bfca" }).color;
}

const CTRY_NEXT_ACTION = {
  "Revenue proven": "Scale — open ROAS by Country",
  "Customer proven": "Confirm revenue attribution",
  "SQL producer": "Review SQL quality",
  "Spend without customer proof": "Review — spend without proof",
  "FX unavailable": "FX incomplete — USD/ROAS withheld",
  "Geo attribution needs review": "Open Revenue Health",
  "No spend in window": "No spend to judge",
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
  root.innerHTML = `
    <div class="dash-tab-intro dash-anim">
      <h2 class="dash-tab-intro__title">Countries &amp; Markets</h2>
      <p class="dash-tab-intro__sub">Where Google Ads spend, SQLs, customers and closed-won revenue are actually showing up. Spend is native GBP; revenue is HubSpot closed-won USD.</p>
    </div>
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
    return '<span class="dash-unavailable">Unavailable</span>';
  }
  const native = escapeHtml(fmtCompactCurrency(row.spend_native, row.native_currency || "GBP"));
  if (row.spend_usd !== null && row.spend_usd !== undefined) {
    return `${native} <span class="ctry-usd">· ${escapeHtml(fmtMoney(row.spend_usd))}</span>`;
  }
  return native;
}

function ctryRoasCell(row) {
  if (row.roas_available && row.roas !== null && row.roas !== undefined) {
    return `<span class="ctry-roas">${escapeHtml(fmtRoasMultiple(row.roas))}</span>`;
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

function renderCtryKpiRow(d) {
  const k = d.kpis || {};
  const pc = d.period_change || {};
  const native = k.verified_spend_native || {};
  const nativeVal = ctryNativeBig(native);
  const cur = escapeHtml(native.currency || "GBP");
  const usdSub = (k.verified_spend_usd !== null && k.verified_spend_usd !== undefined)
    ? `${cur} native · ${escapeHtml(fmtMoney(k.verified_spend_usd))} USD`
    : `${cur} native · USD unavailable (FX)`;

  const cards = [
    {
      key: "spend", label: "Verified Geo Spend",
      value: nativeVal, sub: usdSub,
      delta: dashDeltaChip("verified_spend_usd", pc),
      source: "Google Ads · native GBP · country-attributed",
      ok: native.amount !== null && native.amount !== undefined,
    },
    {
      key: "sqls", label: "SQLs",
      value: dashValue(k.sqls, fmtCount), sub: "Qualified leads · country-attributed",
      delta: dashDeltaChip("sqls", pc),
      source: "HubSpot qualified leads",
      ok: k.sqls !== null && k.sqls !== undefined,
    },
    {
      key: "customers", label: "Customers",
      value: dashValue(k.customers, fmtCount),
      sub: `Closed-won · ${(k.countries_with_customers === null || k.countries_with_customers === undefined) ? "—" : fmtCount(k.countries_with_customers)} countries`,
      delta: dashDeltaChip("customers", pc),
      source: "HubSpot Closed-Won",
      ok: k.customers !== null && k.customers !== undefined,
    },
    {
      key: "revenue", label: "Won Revenue",
      value: dashValue(k.won_revenue_usd, fmtMoney), sub: "USD · HubSpot closed-won",
      delta: dashDeltaChip("won_revenue_usd", pc),
      source: "HubSpot Closed-Won",
      ok: k.won_revenue_usd !== null && k.won_revenue_usd !== undefined,
    },
    {
      key: "roas", label: "Geo ROAS",
      value: (k.geo_roas !== null && k.geo_roas !== undefined)
        ? escapeHtml(fmtRoasMultiple(k.geo_roas))
        : '<span class="dash-unavailable">Unavailable</span>',
      sub: `${(k.countries_with_spend === null || k.countries_with_spend === undefined) ? "—" : fmtCount(k.countries_with_spend)} countries with spend`,
      delta: dashDeltaChip("geo_roas", pc),
      source: "Revenue ÷ verified USD spend",
      ok: k.geo_roas !== null && k.geo_roas !== undefined,
    },
    {
      key: "residual", label: "Residual Revenue",
      value: dashValue(k.residual_revenue_usd, fmtMoney),
      sub: `${(k.residual_customers === null || k.residual_customers === undefined) ? "—" : fmtCount(k.residual_customers)} customers · no country`,
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
// size = customers (falls back to native spend when no customers anywhere so the
// map is never uniformly flat); colour = business status. The residual is NEVER
// mapped — it is not a country.
function ctryMapSizeMetric(rows) {
  // Prefer customers; if no country has customers, fall back to native spend.
  const anyCustomers = rows.some((r) => (r.customers || 0) > 0);
  const metric = anyCustomers
    ? (r) => r.customers || 0
    : (r) => r.spend_native || 0;
  return { metric, label: anyCustomers ? "customers" : "native spend" };
}

function renderCtryGeoMap(d) {
  const rows = (d.countries || []).filter((r) => !r.is_residual);
  const legend = Object.keys(CTRY_STATUS_META).map((s) =>
    `<span class="dash-legend__item"><span class="dash-legend__swatch" style="background:${CTRY_STATUS_META[s].color}"></span>${escapeHtml(s)}</span>`).join("");
  const header = `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Geo Performance Map</h3>
        <p class="dash-panel__sub">Countries grouped by region · bubble size = customers · colour = status · click a market for proof</p>
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
    title.textContent = `${r.country_name || "—"} · ${r.region || ""}`;
    tooltip.appendChild(title);
    const usd = (r.spend_usd !== null && r.spend_usd !== undefined) ? fmtMoney(r.spend_usd) : "Unavailable";
    const roas = (r.roas_available && r.roas !== null && r.roas !== undefined) ? fmtRoasMultiple(r.roas) : "Unavailable";
    [["Spend (£)", r.spend_native !== null && r.spend_native !== undefined ? fmtCompactCurrency(r.spend_native, r.native_currency || "GBP") : "Unavailable"],
     ["Spend (USD)", usd],
     ["SQLs", r.sqls === null || r.sqls === undefined ? "Unavailable" : String(r.sqls)],
     ["Customers", r.customers === null || r.customers === undefined ? "Unavailable" : String(r.customers)],
     ["Revenue", r.won_revenue_usd === null || r.won_revenue_usd === undefined ? "Unavailable" : fmtMoney(r.won_revenue_usd)],
     ["ROAS", roas],
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
  const head = ["Country", "Region", "Spend", "SQLs", "Customers", "Revenue", "ROAS / Status", "Next action"]
    .map((h) => `<th>${escapeHtml(h)}</th>`).join("");
  const body = real.map((r, i) => `
    <tr class="ctry-row" data-ctry-row="${i}" tabindex="0" role="button" aria-label="Open ${escapeHtml(r.country_name || "country")} detail">
      <td class="ctry-td ctry-td--name">
        <span class="ctry-dot ${(CTRY_STATUS_META[r.status] || {}).dot || "ctry-dot--muted"}"></span>${escapeHtml(r.country_name || "—")}
      </td>
      <td class="ctry-td--region">${escapeHtml(r.region || "—")}</td>
      <td class="ctry-num">${ctrySpendCell(r)}</td>
      <td class="ctry-num">${dashValue(r.sqls, fmtCount)}</td>
      <td class="ctry-num">${dashValue(r.customers, fmtCount)}</td>
      <td class="ctry-num">${dashValue(r.won_revenue_usd, fmtMoney)}</td>
      <td>${ctryRoasCell(r)}</td>
      <td class="ctry-next">${escapeHtml(CTRY_NEXT_ACTION[r.status] || "Open ROAS by Country")}</td>
    </tr>`).join("");

  // Residual row — pinned to the bottom, NOT clickable, never a ROAS. It is the
  // isolated "no country" bucket, never treated as a real market.
  let residualRow = "";
  if (residual) {
    residualRow = `
      <tr class="ctry-row ctry-row--residual" aria-label="Unattributed residual (not a country)">
        <td class="ctry-td ctry-td--name"><span class="ctry-dot ctry-dot--muted"></span>${escapeHtml(residual.label || "Unattributed / No Country")}</td>
        <td class="ctry-td--region">—</td>
        <td class="ctry-num">${ctrySpendCell(residual)}</td>
        <td class="ctry-num"><span class="dash-unavailable">Unavailable</span></td>
        <td class="ctry-num">${dashValue(residual.customers, fmtCount)}</td>
        <td class="ctry-num">${dashValue(residual.won_revenue_usd, fmtMoney)}</td>
        <td><span class="ctry-status ctry-status--residual">Unattributed</span></td>
        <td class="ctry-next">Preserved — never distributed</td>
      </tr>`;
  }
  return `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Country Leaderboard</h3>
        <p class="dash-panel__sub">Every market's spend, SQLs, customers and revenue · click a row for proof · ROAS only when spend + FX + revenue + geo reconciliation are safe</p>
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
// customers → native spend); never a fabricated percentage when the denominator
// is missing (a region with an unavailable metric shows no bar).
function _ctryRegionalBarMetric(rows) {
  if (rows.some((r) => (r.won_revenue_usd || 0) > 0)) return "won_revenue_usd";
  if (rows.some((r) => (r.customers || 0) > 0)) return "customers";
  if (rows.some((r) => (r.spend_native || 0) > 0)) return "spend_native";
  return null;
}

function renderCtryRegionalMix(d) {
  const rows = d.regional_mix || [];
  const header = `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Regional Mix</h3>
        <p class="dash-panel__sub">Where spend, customers and revenue concentrate</p>
      </div>
    </div>`;
  if (!rows.length) {
    return `${header}<p class="rev-breakdown-empty">No regional activity in this window.</p>`;
  }
  const metricKey = _ctryRegionalBarMetric(rows);
  const metricLabel = { won_revenue_usd: "revenue", customers: "customers", spend_native: "native spend" }[metricKey] || "activity";
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
          <span>Customers ${dashValue(r.customers, fmtCount)}</span>
          <span>${r.roas_available && r.roas !== null && r.roas !== undefined ? "ROAS " + escapeHtml(fmtRoasMultiple(r.roas)) : escapeHtml(r.status || "—")}</span>
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
        <h3 class="dash-panel__title">Unattributed / No Country</h3>
        <p class="dash-panel__sub">Isolated — never distributed across markets</p>
      </div>
    </div>`;
  if (!res) {
    return `${header}<p class="rev-breakdown-empty">No unattributed residual in this window.</p>`;
  }
  return `${header}
    <dl class="ctry-residual-tiles">
      <div><dt>Residual revenue</dt><dd>${dashValue(res.won_revenue_usd, fmtMoney)}</dd></div>
      <div><dt>Customers</dt><dd>${dashValue(res.customers, fmtCount)}</dd></div>
      <div><dt>SQLs</dt><dd>${dashValue(res.sqls, fmtCount)}</dd></div>
      <div><dt>Geo spend residual</dt><dd>${res.spend_native === null || res.spend_native === undefined ? '<span class="dash-unavailable">Unavailable</span>' : escapeHtml(fmtCompactCurrency(res.spend_native, res.native_currency || "GBP"))}</dd></div>
    </dl>
    <p class="ctry-residual-note">${escapeHtml(res.reason || "Revenue and spend with no country are preserved separately — never spread across real countries.")}</p>`;
}

const CTRY_DEAL_COLUMNS = [
  ["company", "Company"], ["company_id", "Company ID"], ["main_contact", "Main Contact"],
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
    const deals = r.deals || [];
    const dealProofAvailable = d.deal_proof_available !== false;
    const dealHead = CTRY_DEAL_COLUMNS.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("");
    // When the deal ledger is unavailable we cannot prove OR disprove deals — say
    // Unavailable, never "No closed-won deals" (which would imply the country has none).
    const dealBody = !dealProofAvailable
      ? `<tr><td colspan="${CTRY_DEAL_COLUMNS.length}" class="dash-unavailable">Closed-won deal proof unavailable — the deal ledger could not be read (revenue may still be canonical).</td></tr>`
      : deals.length
        ? deals.map((deal) => `<tr>${CTRY_DEAL_COLUMNS.map(([key]) => `<td class="ctry-deal-td ctry-deal-td--${key}">${ctryDealCell(deal, key)}</td>`).join("")}</tr>`).join("")
        : `<tr><td colspan="${CTRY_DEAL_COLUMNS.length}" class="rev-breakdown-empty">No closed-won deals to prove this country in this window.</td></tr>`;

    const links = [
      ["roas-countries", "Open ROAS by Country"],
      ["deals", "Open Deals"],
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
        <strong>${escapeHtml(r.country_name || "Country")}</strong>
        <span class="ctry-drawer__region">${escapeHtml(r.region || "")}</span>
        <span class="ctry-status" style="--ctry-accent:${ctryStatusColor(r.status)}">${escapeHtml(r.status || "")}</span>
        <button type="button" class="ctry-drawer__close" aria-label="Close">×</button>
      </div>
      <dl class="ctry-drawer__summary">
        <div><dt>Spend (native)</dt><dd>${ctrySpendCell(r)}</dd></div>
        <div><dt>SQLs</dt><dd>${dashValue(r.sqls, fmtCount)}</dd></div>
        <div><dt>Customers</dt><dd>${dashValue(r.customers, fmtCount)}</dd></div>
        <div><dt>Revenue</dt><dd>${dashValue(r.won_revenue_usd, fmtMoney)}</dd></div>
        <div><dt>Geo ROAS / status</dt><dd>${ctryRoasCell(r)}</dd></div>
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

// ── Dashboard — Deals, Opportunities & Pipeline Intelligence tab (PR-ADS-139) ─
//
// Sixth and final dashboard tab. Reuses the PR-134/135/136/137/138 design system
// end to end (KPI cards, funnel, decision cards, truth footer, drawers, loading/
// refetch/unavailable states) — NO new visual language. It answers: what happens
// after an SQL — opportunity creation, closed-won revenue, and which SQLs have not
// yet become customers. Truth doctrine: HubSpot closed-won is revenue truth (USD);
// open pipeline is NOT revenue; opportunities are NOT customers; NO ROAS here. The
// durable ledger stores closed-won ONLY, so open pipeline / aging / closed-lost /
// opportunity rates render "Unavailable" — never invented.

let dashDeals = null;
let dashDealsStatus = "loading";

const DEAL_STATUS_META = {
  "Revenue proven": { color: "#22b07d", dot: "deal-dot--proven" },
  "SQL producer": { color: "#129ef5", dot: "deal-dot--sql" },
  "SQL — not yet a customer": { color: "#ff8a3d", dot: "deal-dot--pending" },
  "Aging SQL — needs sales review": { color: "#e0564e", dot: "deal-dot--aging" },
  "Amount unavailable": { color: "#8896a6", dot: "deal-dot--muted" },
  "Attribution needs review": { color: "#7c5cff", dot: "deal-dot--attr" },
  "Stage unavailable": { color: "#b6bfca", dot: "deal-dot--muted" },
  "No activity in window": { color: "#b6bfca", dot: "deal-dot--muted" },
};
function dealStatusColor(status) {
  return (DEAL_STATUS_META[status] || { color: "#b6bfca" }).color;
}
function dealStatusDot(status) {
  return (DEAL_STATUS_META[status] || {}).dot || "deal-dot--muted";
}

// Conversion rate (fraction 0..1) → "%", or Unavailable — never a fabricated 0%.
function dealRate(v) {
  if (v === null || v === undefined) return '<span class="dash-unavailable">Unavailable</span>';
  return escapeHtml((v * 100).toFixed(1) + "%");
}

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
  root.innerHTML = `
    <div class="dash-tab-intro dash-anim">
      <h2 class="dash-tab-intro__title">Deals &amp; Pipeline</h2>
      <p class="dash-tab-intro__sub">What happens after SQL: opportunity creation, open pipeline, closed-won revenue, lost deals, and stuck opportunities. Open pipeline is not revenue.</p>
    </div>
    ${renderDealKpiRow(d)}
    <section class="dash-panel dash-anim deal-funnel-panel" aria-label="Pipeline funnel">
      ${renderDealFunnel(d)}
    </section>
    <div class="dash-main-grid deal-mid-grid">
      <section class="dash-panel dash-anim deal-source-panel" aria-label="Source to pipeline">
        ${renderDealSourcePipeline(d)}
      </section>
      <aside class="dash-panel dash-anim deal-aging-panel" aria-label="Opportunity aging">
        ${renderDealAging(d)}
      </aside>
    </div>
    <section class="dash-panel dash-anim deal-campaign-panel" aria-label="Campaign to pipeline">
      ${renderDealCampaignPipeline(d)}
    </section>
    <section class="dash-panel dash-anim deal-table-panel" aria-label="Closed-won deals">
      ${renderDealTable(d)}
    </section>
    <section class="dash-panel dash-anim deal-sql-panel" aria-label="SQLs not yet closed-won">
      ${renderDealSqlNoDeal(d)}
    </section>
    <section class="dash-decisions dash-anim" aria-label="Pipeline decision cards">
      ${renderDashDecisionCards(d)}
    </section>
    ${renderDealTruthFooter(d)}
  `;
  wireDealDrawers(root, d);
}

function renderDealKpiRow(d) {
  const k = d.kpis || {};
  const pc = d.period_change || {};
  const cards = [
    { key: "sqls", label: "SQLs", value: dashValue(k.sqls, fmtCount),
      sub: "Qualified leads · this window", delta: dashDeltaChip("sqls", pc),
      source: "HubSpot qualified leads", ok: k.sqls !== null && k.sqls !== undefined },
    { key: "opps", label: "Opportunities Created",
      value: '<span class="dash-unavailable">Unavailable</span>',
      sub: "Open opportunity stage not in ledger", delta: '<span class="dash-delta dash-delta--none">No comparison</span>',
      source: "HubSpot deal ledger", ok: false },
    { key: "open", label: "Open Pipeline",
      value: '<span class="dash-unavailable">Unavailable</span>',
      sub: "Open deals only — not revenue", delta: '<span class="dash-delta dash-delta--none">No comparison</span>',
      source: "HubSpot deal ledger", ok: false },
    { key: "revenue", label: "Closed-Won Revenue", value: dashValue(k.closed_won_revenue_usd, fmtMoney),
      sub: "HubSpot closed-won USD", delta: dashDeltaChip("closed_won_revenue_usd", pc),
      source: "HubSpot Closed-Won", ok: k.closed_won_revenue_usd !== null && k.closed_won_revenue_usd !== undefined },
    { key: "customers", label: "Customers", value: dashValue(k.closed_won_customers, fmtCount),
      sub: "Closed-won · this window", delta: dashDeltaChip("closed_won_customers", pc),
      source: "HubSpot Closed-Won", ok: k.closed_won_customers !== null && k.closed_won_customers !== undefined },
    { key: "sql2cust", label: "SQL → Customer Rate", value: dealRate(k.sql_to_customer_rate),
      sub: "Closed-won ÷ SQLs · full funnel", delta: dashDeltaChip("sql_to_customer_rate", pc),
      source: "HubSpot", ok: k.sql_to_customer_rate !== null && k.sql_to_customer_rate !== undefined },
    { key: "opp2cust", label: "Opportunity → Customer Rate",
      value: '<span class="dash-unavailable">Unavailable</span>',
      sub: "Opportunity stage not in ledger", delta: '<span class="dash-delta dash-delta--none">No comparison</span>',
      source: "HubSpot deal ledger", ok: false },
    { key: "notwon", label: "SQLs Not Yet Closed-Won", value: dashValue(k.sqls_not_closed_won, fmtCount),
      sub: "Qualified · no closed-won deal", delta: '<span class="dash-delta dash-delta--none">Sales gap</span>',
      source: "Open/lost pipeline not visible", ok: k.sqls_not_closed_won !== null && k.sqls_not_closed_won !== undefined },
  ];

  const compareNote = pc.available
    ? `<p class="dash-compare-note">Change ${escapeHtml(pc.label || "vs previous period")}${pc.note ? ` — ${escapeHtml(pc.note)}` : ""}</p>`
    : `<p class="dash-compare-note dash-compare-note--muted">No previous-period comparison${pc.reason ? ` — ${escapeHtml(pc.reason)}` : ""}</p>`;

  return `
    <div class="dash-kpi-grid deal-kpi-grid">
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

function renderDealFunnel(d) {
  const stages = d.funnel || [];
  const header = `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Pipeline Funnel</h3>
        <p class="dash-panel__sub">SQL → Opportunities → Open Pipeline → Closed Won → Closed Lost · open pipeline is not revenue</p>
      </div>
    </div>`;
  if (!stages.length) {
    return `${header}<p class="rev-breakdown-empty">No pipeline activity in this window.</p>`;
  }
  const cells = stages.map((s, i) => {
    const unavailable = s.status === "Stage unavailable";
    const countHtml = unavailable
      ? '<span class="dash-unavailable">Unavailable</span>'
      : dashValue(s.count, fmtCount);
    const amount = (s.amount_usd !== null && s.amount_usd !== undefined)
      ? `<div class="deal-funnel__amt">${escapeHtml(fmtMoney(s.amount_usd))}</div>` : "";
    const rate = (s.rate_from_previous !== null && s.rate_from_previous !== undefined)
      ? `<span class="deal-funnel__conv">→ ${dealRate(s.rate_from_previous)}</span>` : "";
    const arrowFallback = '<span aria-hidden="true">→</span>';
    const conv = i > 0 ? `<div class="deal-funnel__arrow">${rate || arrowFallback}</div>` : "";
    return `
      ${conv}
      <div class="deal-funnel__stage ${unavailable ? "deal-funnel__stage--unavailable" : ""}" style="--deal-accent:${dealStatusColor(s.status)}">
        <div class="deal-funnel__label">${escapeHtml(s.stage)}</div>
        <div class="deal-funnel__value">${countHtml}</div>
        ${amount}
        <div class="deal-funnel__status"><span class="deal-dot ${dealStatusDot(s.status)}"></span>${escapeHtml(s.status)}</div>
      </div>`;
  }).join("");
  const note = d.pipeline_stage_data_available === false
    ? `<p class="deal-stage-note">${escapeHtml(d.pipeline_stage_reason || "Open opportunity stage data is unavailable from the durable HubSpot deal ledger.")}</p>`
    : "";
  return `${header}<div class="deal-funnel">${cells}</div>${note}`;
}

function renderDealAging(d) {
  const buckets = d.aging_buckets || [];
  const header = `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Opportunity Aging</h3>
        <p class="dash-panel__sub">Open opportunities by age</p>
      </div>
    </div>`;
  const rows = buckets.map((b) => `
    <li class="deal-aging-row">
      <span class="deal-aging-row__bucket">${escapeHtml(b.bucket)}</span>
      <span class="deal-aging-row__count">${b.open_opportunities === null || b.open_opportunities === undefined ? '<span class="dash-unavailable">Unavailable</span>' : dashValue(b.open_opportunities, fmtCount)}</span>
      <span class="deal-aging-row__amt">${b.open_pipeline_usd === null || b.open_pipeline_usd === undefined ? '<span class="dash-unavailable">Unavailable</span>' : escapeHtml(fmtMoney(b.open_pipeline_usd))}</span>
    </li>`).join("");
  const note = `<p class="deal-stage-note">Opportunity aging needs open deals and deal create dates, which are not stored in the durable HubSpot deal ledger — shown as Unavailable, never fabricated.</p>`;
  return `${header}<ul class="deal-aging-list">${rows}</ul>${note}`;
}

function dealPipelineCell(row) {
  // Open pipeline is structurally unavailable on this tab.
  return '<span class="dash-unavailable">Unavailable</span>';
}

function renderDealSourcePipeline(d) {
  const rows = d.source_breakdown || [];
  const header = `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Source → Pipeline</h3>
        <p class="dash-panel__sub">Which source creates customers &amp; revenue · no ROAS here</p>
      </div>
    </div>`;
  if (!rows.length) {
    return `${header}<p class="rev-breakdown-empty">No source activity in this window.</p>`;
  }
  const head = ["Source", "SQLs", "Open Pipeline", "Customers", "Revenue", "SQL → Customer", "Status"]
    .map((h) => `<th>${escapeHtml(h)}</th>`).join("");
  const body = rows.map((r) => `
    <tr>
      <td class="deal-td--name"><span class="deal-dot ${dealStatusDot(r.status)}"></span>${escapeHtml(r.source_detail || "—")}</td>
      <td class="deal-num">${dashValue(r.sqls, fmtCount)}</td>
      <td class="deal-num">${dealPipelineCell(r)}</td>
      <td class="deal-num">${dashValue(r.closed_won_customers, fmtCount)}</td>
      <td class="deal-num">${dashValue(r.closed_won_revenue_usd, fmtMoney)}</td>
      <td class="deal-num">${dealRate(r.sql_to_customer_rate)}</td>
      <td><span class="deal-status" style="--deal-accent:${dealStatusColor(r.status)}">${escapeHtml(r.status || "—")}</span></td>
    </tr>`).join("");
  return `${header}
    <div class="deal-scroll">
      <table class="deal-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>
    </div>`;
}

function renderDealCampaignPipeline(d) {
  const rows = d.campaign_breakdown || [];
  const linkCampaigns = dashCanNavigate("dashboard")
    ? '<a class="deal-link" href="#/dashboard?tab=campaigns">Open Campaigns tab →</a>' : "";
  const linkRoas = dashCanNavigate("roas-campaigns")
    ? '<a class="deal-link" href="#/roas-campaigns">Open ROAS by Campaign →</a>' : "";
  const header = `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Campaign → Pipeline</h3>
        <p class="dash-panel__sub">Google Ads campaigns · SQLs → customers → closed-won revenue · ROAS lives on the spend pages</p>
      </div>
    </div>`;
  if (!rows.length) {
    return `${header}<p class="rev-breakdown-empty">No campaign pipeline activity in this window.</p>`;
  }
  const head = ["Campaign", "SQLs", "Open Pipeline", "Customers", "Revenue", "SQL → Customer", "Status"]
    .map((h) => `<th>${escapeHtml(h)}</th>`).join("");
  const body = rows.map((r) => `
    <tr>
      <td class="deal-td--name"><span class="deal-dot ${dealStatusDot(r.status)}"></span>${escapeHtml(r.campaign_name || "—")}</td>
      <td class="deal-num">${dashValue(r.sqls, fmtCount)}</td>
      <td class="deal-num">${dealPipelineCell(r)}</td>
      <td class="deal-num">${dashValue(r.closed_won_customers, fmtCount)}</td>
      <td class="deal-num">${dashValue(r.closed_won_revenue_usd, fmtMoney)}</td>
      <td class="deal-num">${dealRate(r.sql_to_customer_rate)}</td>
      <td><span class="deal-status" style="--deal-accent:${dealStatusColor(r.status)}">${escapeHtml(r.status || "—")}</span></td>
    </tr>`).join("");
  return `${header}
    <div class="deal-scroll">
      <table class="deal-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>
    </div>
    <div class="deal-panel-foot">${linkCampaigns}${linkRoas}</div>`;
}

function renderDealTable(d) {
  const rows = d.deals || [];
  const header = `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">Closed-Won Deals</h3>
        <p class="dash-panel__sub">Revenue proof · click a deal for detail · open / lost deals are not in the durable ledger</p>
      </div>
    </div>`;
  if (d.deal_proof_available === false) {
    return `${header}<p class="deal-stage-note">Closed-won deal proof is unavailable — ${d.truth_status && d.truth_status.deals === "blocked" ? "the revenue integration is disconnected" : "the deal ledger could not be read"}.</p>`;
  }
  if (!rows.length) {
    return `${header}<p class="rev-breakdown-empty">No closed-won deals in this window.</p>`;
  }
  const head = ["Deal / Company", "Stage", "Amount", "Age", "Close Date", "Campaign", "Country", "Status"]
    .map((h) => `<th>${escapeHtml(h)}</th>`).join("");
  const body = rows.map((r, i) => `
    <tr class="deal-row" data-deal-row="${i}" tabindex="0" role="button" aria-label="Open ${escapeHtml(r.company || "deal")} detail">
      <td class="deal-td--name"><span class="deal-dot ${dealStatusDot(r.status)}"></span>${dashValue(r.company)}</td>
      <td>${dashValue(r.stage)}</td>
      <td class="deal-num">${dashValue(r.amount_usd, fmtMoney)}</td>
      <td class="deal-num">${dashValue(r.age_days, fmtCount)}</td>
      <td>${dashValue(r.close_date)}</td>
      <td>${dashValue(r.campaign_name)}</td>
      <td>${dashValue(r.country)}</td>
      <td><span class="deal-status" style="--deal-accent:${dealStatusColor(r.status)}">${escapeHtml(r.status || "—")}</span></td>
    </tr>`).join("");
  return `${header}
    <div class="deal-scroll">
      <table class="deal-table deal-table--rows"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>
    </div>`;
}

const DEAL_DRAWER_FIELDS = [
  ["company", "Company"], ["company_id", "Company ID"], ["main_contact", "Main Contact"],
  ["contact_id", "Contact ID"], ["deal_name", "Deal"], ["deal_id", "Deal ID"],
  ["amount_usd", "Amount"], ["stage", "Stage"], ["stage_group", "Stage group"],
  ["created_date", "Created date"], ["close_date", "Close date"], ["age_days", "Age (days)"],
  ["days_to_close", "Days to close"], ["source_group", "Source group"], ["source_detail", "Source detail"],
  ["campaign_name", "Campaign / source"], ["country", "Country"], ["attribution_status", "Attribution"],
];

function dealDrawerCell(deal, key) {
  if (key === "amount_usd") return dashValue(deal.amount_usd, fmtMoney); // null → Unavailable, never $0
  if (key === "age_days" || key === "days_to_close") return dashValue(deal[key], fmtCount);
  return dashValue(deal[key]);
}

function dealRecommendedReview(deal) {
  if (deal.amount_usd === null || deal.amount_usd === undefined)
    return "Amount missing — pipeline value unavailable for this deal.";
  return "Closed won — revenue proof. Open pipeline / aging is not tracked in the durable ledger.";
}

function wireDealDrawers(root, d) {
  const rows = d.deals || [];
  const open = (i) => {
    const r = rows[i];
    if (!r) return;
    let drawer = root.querySelector(".deal-drawer");
    if (!drawer) {
      drawer = document.createElement("div");
      drawer.className = "deal-drawer";
      root.querySelector(".deal-table-panel").appendChild(drawer);
    }
    const tiles = DEAL_DRAWER_FIELDS.map(([key, label]) =>
      `<div><dt>${escapeHtml(label)}</dt><dd>${dealDrawerCell(r, key)}</dd></div>`).join("");
    const links = [
      ["roas-campaigns", "Open ROAS by Campaign"],
      ["deals", "Open Deals ledger"],
      ["revenue-health", "Open Revenue Health"],
    ].filter(([page]) => dashCanNavigate(page))
      .map(([page, label]) => `<a class="deal-drawer__link" href="#/${escapeHtml(page)}">${escapeHtml(label)} →</a>`).join("");
    drawer.innerHTML = `
      <div class="deal-drawer__head">
        <strong>${escapeHtml(r.company || "Deal")}</strong>
        <span class="deal-status" style="--deal-accent:${dealStatusColor(r.status)}">${escapeHtml(r.status || "")}</span>
        <button type="button" class="deal-drawer__close" aria-label="Close">×</button>
      </div>
      <div class="deal-drawer__section">
        <h4 class="deal-drawer__h">Deal &amp; attribution proof</h4>
        <dl class="deal-drawer__grid">${tiles}</dl>
      </div>
      <div class="deal-drawer__section">
        <h4 class="deal-drawer__h">Recommended review</h4>
        <p class="deal-review-note">${escapeHtml(dealRecommendedReview(r))}</p>
      </div>
      <div class="deal-drawer__actions">${links}</div>`;
    drawer.querySelector(".deal-drawer__close").addEventListener("click", () => drawer.remove());
    drawer.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  root.querySelectorAll(".deal-row").forEach((tr) => {
    tr.addEventListener("click", () => open(Number(tr.dataset.dealRow)));
    tr.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(Number(tr.dataset.dealRow)); }
    });
  });
}

function renderDealSqlNoDeal(d) {
  const rows = d.sql_no_deal || [];
  const total = d.sql_no_deal_total || 0;
  const header = `
    <div class="dash-panel__header">
      <div>
        <h3 class="dash-panel__title">SQLs Not Yet Closed-Won</h3>
        <p class="dash-panel__sub">Qualified SQLs with no closed-won deal · <strong>read-only</strong> — open / lost pipeline is not visible</p>
      </div>
    </div>`;
  if (d.sql_no_deal_available === false) {
    return `${header}<p class="deal-stage-note">Per-SQL linkage is unavailable — the qualified-lead source could not be read for this window.</p>`;
  }
  if (!rows.length) {
    return `${header}<p class="rev-breakdown-empty">Every qualified SQL has a closed-won deal in this window.</p>`;
  }
  const head = ["Company / Contact", "SQL Date", "Days Since SQL", "Campaign", "Country", "Status"]
    .map((h) => `<th>${escapeHtml(h)}</th>`).join("");
  const body = rows.map((r) => `
    <tr>
      <td class="deal-td--name"><span class="deal-dot ${dealStatusDot(r.status)}"></span>${dashValue(r.company)}</td>
      <td>${dashValue(r.sql_date)}</td>
      <td class="deal-num">${dashValue(r.days_since_sql, fmtCount)}</td>
      <td>${dashValue(r.campaign_name)}</td>
      <td>${dashValue(r.country)}</td>
      <td><span class="deal-status" style="--deal-accent:${dealStatusColor(r.status)}">${escapeHtml(r.status || "—")}</span></td>
    </tr>`).join("");
  const more = total > rows.length
    ? `<p class="deal-stage-note">Showing ${escapeHtml(fmtCount(rows.length))} of ${escapeHtml(fmtCount(total))} qualified SQLs with no closed-won deal.</p>` : "";
  return `${header}
    <div class="deal-scroll">
      <table class="deal-table deal-table--rows"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>
    </div>${more}`;
}

function renderDealTruthFooter(d) {
  const ts = d.truth_status || {};
  const chip = (label, status) => `
    <span class="dash-footer-chip" title="${escapeHtml(`${label}: ${status || "unknown"}`)}">
      <span class="dash-dot ${DASH_STATUS_DOT[status] || "dash-dot--muted"}"></span>${escapeHtml(label)}
    </span>`;
  const allGood = ts.sqls === "ready" && ts.deals === "ready"
    && ts.amounts === "ready" && ts.attribution === "ready";
  return `
    <footer class="dash-truth-footer ${allGood ? "dash-truth-footer--ok" : ""}">
      ${chip("SQLs", ts.sqls)}
      ${chip("Deals", ts.deals)}
      ${chip("Stages", ts.stages)}
      ${chip("Amounts", ts.amounts)}
      ${chip("Attribution", ts.attribution)}
      <span class="dash-footer-chip dash-footer-chip--readonly">Read-only</span>
      ${allGood
        ? '<span class="dash-footer-note">Pipeline truth verified — open pipeline is separate from closed-won revenue.</span>'
        : (dashCanNavigate("revenue-health")
          ? '<a class="dash-footer-note" href="#/revenue-health">Some pipeline proof needs review — open Revenue Health</a>'
          : '<span class="dash-footer-note">Some pipeline proof needs review — contact your administrator.</span>')}
    </footer>`;
}

// ── Campaigns page ─────────────────────────────────────────────────────────

// ── Campaign Evidence page (PR-ADS-143) ─────────────────────────────────────
// GENUINE selected-window campaign evidence: canonical Google Ads spend (native
// GBP + FX-safe USD) reconciled with durable, deduplicated HubSpot lead outcomes
// for the SAME window. The selected Evidence Window controls the ACTUAL metrics;
// all_time = genuine cumulative totals (never a scheduler snapshot). Each row
// carries a factual, window-safe Outcome status — never a SCALE/HOLD/FIX/CUT
// action verdict. Read-only. Missing values render Unavailable / N/A / — (never a
// fabricated 0). Source + methodology live in the Help drawer, not above the table.

let _campaignEvidence = [];       // selected-window campaign rows from /api/campaigns
let _campaignSummary  = null;     // KPI summary (reconciled to canonical totals)
let _campaignAudit    = null;     // reconciliation/audit metadata (diagnostic, not shown)
let _campaignMeta     = { window: null, spend_currency: "GBP", reporting_currency: "USD" };
let _campaignLoadState = "idle";  // idle | loading | ok | empty | db_unavailable | error
const _campaignFilters = { search: "", status: "all", outcome: "all", sort: "spend" };

// Factual outcome-status → badge tone (no action verdict).
const CAMPAIGN_STATUS_TONE = {
  "SQL producer": "good",
  "Junk-heavy": "bad",
  "Spend without SQL proof": "warn",
  "Mapping review": "info",
  "No outcome evidence": "muted",
  "Data unavailable": "muted",
};

function campaignStatusBadge(status) {
  const tone = CAMPAIGN_STATUS_TONE[status] || "muted";
  return `<span class="campaign-status-badge campaign-status-badge--${tone}">${escapeHtml(status || "—")}</span>`;
}

// GBP formatter (mirrors fmtDollar's compaction, £ instead of $).
function fmtGbp(n) {
  if (n === null || n === undefined) return "—";
  if (n >= 1000) return "£" + (n / 1000).toFixed(1) + "k";
  return "£" + n.toFixed(0);
}

const CAMPAIGN_WINDOW_LABELS = {
  "7d": "Last 7 days", "14d": "Last 14 days", "30d": "Last 30 days",
  "60d": "Last 60 days", "180d": "Last 180 days", "all_time": "All time",
};

async function loadCampaignEvidence() {
  renderPageDatasetFreshness("campaigns");
  const shell = document.getElementById("campaign-evidence-shell");
  if (!shell) return;
  shell.innerHTML = `<p class="empty-state" style="padding:var(--space-5)">Loading campaign evidence…</p>`;
  _campaignLoadState = "loading";

  try {
    const data = await fetchJSON(`/api/campaigns?${evidenceWindowQuery()}`);
    _campaignEvidence = data.campaigns || [];
    _campaignSummary  = data.summary || null;
    _campaignAudit    = data.audit || null;
    _campaignMeta = {
      window: data.window || getEvidenceWindow(),
      spend_currency: data.spend_currency || "GBP",
      reporting_currency: data.reporting_currency || "USD",
    };
    _campaignLoadState = data.db_unavailable ? "db_unavailable"
      : (_campaignEvidence.length ? "ok" : "empty");
    renderCampaignEvidencePage();
  } catch (_) {
    _campaignLoadState = "error";
    shell.innerHTML = `
      <div class="evidence-empty-state">
        Campaign evidence is unavailable.<br>The underlying dataset could not be read.
      </div>`;
  }
}
// Router entry (kept for compatibility).
function loadCampaigns() { return loadCampaignEvidence(); }

function campaignEvidenceHeader() {
  const label = CAMPAIGN_WINDOW_LABELS[_campaignMeta.window] || _campaignMeta.window || "";
  return `
    <header class="campaign-evidence-head">
      <div class="campaign-evidence-head__text">
        <h2 class="campaign-evidence-title">Campaign Evidence</h2>
        <p class="campaign-evidence-sub">Selected-window Google Ads spend reconciled with HubSpot lead-quality outcomes.</p>
      </div>
      ${label ? `<span class="campaign-evidence-window" title="Selected evidence window">${escapeHtml(label)}</span>` : ""}
    </header>`;
}

function renderCampaignEvidencePage() {
  const shell = document.getElementById("campaign-evidence-shell");
  if (!shell) return;

  if (_campaignLoadState === "db_unavailable") {
    shell.innerHTML = `${campaignEvidenceHeader()}
      <div class="evidence-empty-state">Campaign evidence is unavailable.<br>The underlying dataset could not be read.</div>`;
    return;
  }
  if (_campaignLoadState === "empty") {
    const emptyBody = buildEmptyState({
      pageKey: "campaigns",
      canonicalStatus: getPageCanonicalStatus("campaigns"),
      rowsInWindow: 0,
    });
    shell.innerHTML = `${campaignEvidenceHeader()}
      <div class="evidence-empty-state">No campaign evidence is available in this window.
        <div style="margin-top:8px"><a href="#/health">Open System Status →</a></div></div>
      ${emptyBody}`;
    return;
  }

  shell.innerHTML = `
    ${campaignEvidenceHeader()}
    ${renderCampaignEvidenceKPIs()}
    ${renderCampaignEvidenceFilters()}
    <div class="evidence-table-shell"><div class="evidence-table-scroll" id="campaign-decision-table"></div></div>
  `;
  renderCampaignDecisionTable();
  wireCampaignEvidenceControls();
}

function renderCampaignEvidenceKPIs() {
  const s = _campaignSummary || {};
  const cur = s.spend_currency || _campaignMeta.spend_currency || "GBP";
  // Spend KPI: reporting USD primary (Unavailable when FX incomplete), native GBP
  // subline. Reconciles EXACTLY with canonical spend for the window.
  const spendPrimary = s.spend_usd != null
    ? fmtDollar(s.spend_usd)
    : `<span class="detail-unavailable" title="USD unavailable — FX coverage incomplete for this window">Unavailable</span>`;
  const spendSub = s.spend_native != null
    ? `${fmtGbp(s.spend_native)} ${escapeHtml(cur)}`
    : "native spend unavailable";
  const cpql = s.overall_cpql_usd != null ? fmtDollar(s.overall_cpql_usd)
    : `<span class="detail-unavailable">N/A</span>`;
  // CPQL label — the numerator is ALL canonical Google Ads USD spend; the
  // denominator is mapped SQLs. Label it honestly by coverage (never "mapped
  // campaigns only", which would misdescribe the total-spend numerator). Tooltip
  // discloses the unmatched + not-Google-Ads SQLs excluded from the denominator.
  const cov = s.mapping_coverage || {};
  const cpqlSub = s.overall_cpql_scope === "mapped_only"
    ? `<span class="detail-unavailable" title="Denominator excludes ${fmtCount(cov.unmatched_sqls)} unmatched + ${fmtCount(cov.excluded_not_google_sqls)} Not-Google-Ads SQLs">All Google Ads spend ÷ mapped SQLs</span>`
    : "Google Ads spend ÷ confirmed SQLs";
  return `
    <div class="evidence-kpi-grid campaign-kpi-grid">
      <div class="dash-kpi-card"><div class="dash-kpi-card__label">Campaigns</div>
        <div class="dash-kpi-card__value">${fmtCount(s.campaigns)}</div>
        <div class="dash-kpi-card__sub">With spend or lead evidence</div></div>
      <div class="dash-kpi-card"><div class="dash-kpi-card__label">Spend</div>
        <div class="dash-kpi-card__value">${spendPrimary}</div>
        <div class="dash-kpi-card__sub">${spendSub}</div></div>
      <div class="dash-kpi-card"><div class="dash-kpi-card__label">Confirmed SQLs</div>
        <div class="dash-kpi-card__value">${fmtCount(s.confirmed_sqls_total)}</div>
        <div class="dash-kpi-card__sub">Mapped Google Ads, this window</div></div>
      <div class="dash-kpi-card"><div class="dash-kpi-card__label">Confirmed Junk</div>
        <div class="dash-kpi-card__value">${fmtCount(s.confirmed_junk_total)}</div>
        <div class="dash-kpi-card__sub">Excludes wrong-fit</div></div>
      <div class="dash-kpi-card"><div class="dash-kpi-card__label">Overall CPQL</div>
        <div class="dash-kpi-card__value">${cpql}</div>
        <div class="dash-kpi-card__sub">${cpqlSub}</div></div>
    </div>`;
}

function renderCampaignEvidenceFilters() {
  const f = _campaignFilters;
  const statusOpt = (v, label) =>
    `<option value="${v}"${f.status === v ? " selected" : ""}>${label}</option>`;
  return `
    <div class="campaign-controls">
      <input type="search" id="campaign-search" class="waste-search-input" placeholder="Search campaign name…" aria-label="Search campaign name" value="${escapeHtml(f.search)}">
      <select id="campaign-status" class="waste-filter-select" aria-label="Outcome status">
        ${statusOpt("all", "All statuses")}
        ${statusOpt("SQL producer", "SQL producer")}
        ${statusOpt("Junk-heavy", "Junk-heavy")}
        ${statusOpt("Spend without SQL proof", "Spend without SQL proof")}
        ${statusOpt("Mapping review", "Mapping review")}
        ${statusOpt("No outcome evidence", "No outcome evidence")}
        ${statusOpt("Data unavailable", "Data unavailable")}
      </select>
      <select id="campaign-outcome" class="waste-filter-select" aria-label="Outcome signal">
        <option value="all"${f.outcome === "all" ? " selected" : ""}>All outcomes</option>
        <option value="has_sql"${f.outcome === "has_sql" ? " selected" : ""}>Has confirmed SQL</option>
        <option value="no_sql"${f.outcome === "no_sql" ? " selected" : ""}>No confirmed SQL</option>
        <option value="has_junk"${f.outcome === "has_junk" ? " selected" : ""}>Has confirmed junk</option>
      </select>
      <select id="campaign-sort" class="waste-filter-select" aria-label="Sort campaigns">
        <option value="spend"${f.sort === "spend" ? " selected" : ""}>Highest spend</option>
        <option value="sqls"${f.sort === "sqls" ? " selected" : ""}>Most SQLs</option>
        <option value="junk"${f.sort === "junk" ? " selected" : ""}>Highest confirmed junk</option>
        <option value="junk_rate"${f.sort === "junk_rate" ? " selected" : ""}>Highest junk rate</option>
        <option value="cpql"${f.sort === "cpql" ? " selected" : ""}>Highest CPQL</option>
        <option value="name"${f.sort === "name" ? " selected" : ""}>Campaign name</option>
      </select>
      <button type="button" id="campaign-clear-filters" class="revenue-filter-chip campaign-clear-btn">Reset</button>
    </div>`;
}

function filterCampaignEvidence(rows) {
  const f = _campaignFilters;
  return rows.filter((c) => {
    if (f.search && !(c.campaign_name || "").toLowerCase().includes(f.search.toLowerCase())) return false;
    if (f.status !== "all" && (c.outcome_status || "") !== f.status) return false;
    // Outcome filters match genuine recorded values only — an unavailable (null)
    // metric is never classified as a zero-outcome campaign.
    const sqls = c.confirmed_sqls;
    const junk = c.confirmed_junk;
    if (f.outcome === "has_sql"  && !(sqls != null && sqls > 0))  return false;
    if (f.outcome === "no_sql"   && !(sqls != null && sqls === 0)) return false;
    if (f.outcome === "has_junk" && !(junk != null && junk > 0))  return false;
    return true;
  });
}

// Descending comparator placing null/unavailable values LAST (never coerced to 0).
function _campDescNullLast(getter) {
  return (a, b) => {
    const va = getter(a), vb = getter(b);
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    return vb - va;
  };
}

function sortCampaignEvidence(rows) {
  const by = _campaignFilters.sort;
  const arr = [...rows];
  // Spend sort ranks by the native amount (always present when spend exists);
  // unavailable spend sinks last.
  if (by === "spend")      arr.sort(_campDescNullLast((c) => c.spend_native));
  else if (by === "sqls")  arr.sort(_campDescNullLast((c) => c.confirmed_sqls));
  else if (by === "junk")  arr.sort(_campDescNullLast((c) => c.confirmed_junk));
  else if (by === "junk_rate") arr.sort(_campDescNullLast((c) => c.junk_rate_pct));
  else if (by === "cpql")  arr.sort(_campDescNullLast((c) => c.cpql_usd));
  else if (by === "name")  arr.sort((a, b) => (a.campaign_name || "").localeCompare(b.campaign_name || ""));
  return arr;
}

function renderCampaignDecisionTable() {
  const host = document.getElementById("campaign-decision-table");
  if (!host) return;
  const rows = sortCampaignEvidence(filterCampaignEvidence(_campaignEvidence));
  if (rows.length === 0) {
    host.innerHTML = `<div class="evidence-empty-state">No campaigns match the current filters.</div>`;
    return;
  }
  const thead = `
    <thead>
      <tr>
        <th class="td--name">Campaign</th>
        <th>Status</th>
        <th class="td--num">Spend</th>
        <th class="td--num">Leads</th>
        <th class="td--num">SQLs</th>
        <th class="td--num">Junk</th>
        <th class="td--num">Junk Rate</th>
        <th class="td--num">CPQL</th>
        <th class="td--action"><span class="sr-only">Open evidence</span></th>
      </tr>
    </thead>`;
  const body = rows.map(renderCampaignEvidenceRow).join("");
  host.innerHTML = `<table class="data-table campaign-decision-table">${thead}<tbody>${body}</tbody></table>`;
  host.querySelectorAll("[data-campaign-open]").forEach((el) => {
    const open = () => openCampaignDrawer(
      decodeURIComponent(el.getAttribute("data-campaign-open")),
      el.getAttribute("data-campaign-key") || null);
    el.addEventListener("click", (e) => {
      if (e.target.closest("a")) return;
      open();
    });
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });
  });
}

// Spend cell: reporting USD primary + native GBP subline; falls back to native
// GBP when USD is FX-withheld; "Unavailable" when there is no canonical spend row.
function campaignSpendCell(c) {
  if (c.spend_usd != null) {
    const sub = c.spend_native != null
      ? `<span class="td-sub">${fmtGbp(c.spend_native)} ${escapeHtml(c.spend_currency || "GBP")}</span>` : "";
    return `${fmtDollar(c.spend_usd)}${sub}`;
  }
  if (c.spend_native != null) {
    return `${fmtGbp(c.spend_native)} ${escapeHtml(c.spend_currency || "GBP")}<span class="td-sub">USD unavailable (FX)</span>`;
  }
  return `<span class="detail-unavailable">Unavailable</span>`;
}

function renderCampaignEvidenceRow(c) {
  const jr = c.junk_rate_pct;
  const junkCls = jr == null ? "" :
                  jr < uiThresholds.junk_rate.low_pct   ? "junk--low" :
                  jr <= uiThresholds.junk_rate.high_pct ? "junk--mid" : "junk--high";
  const junkRateStr = jr != null ? jr.toFixed(1) + "%" : "—";
  const leads = c.total_leads == null ? `<span class="detail-unavailable">—</span>` : fmtCount(c.total_leads);
  const sqls  = c.confirmed_sqls == null ? `<span class="detail-unavailable">—</span>` : fmtCount(c.confirmed_sqls);
  const junk  = c.confirmed_junk == null ? `<span class="detail-unavailable">—</span>` : fmtCount(c.confirmed_junk);
  // CPQL: "—" when SQLs unavailable, "N/A" for a genuine zero, else USD value.
  const cpql = c.confirmed_sqls == null ? "—"
    : (c.confirmed_sqls === 0 ? "N/A"
    : (c.cpql_usd != null ? fmtDollar(c.cpql_usd) : "—"));
  const nameEnc = encodeURIComponent(c.campaign_name || "");
  // Stable key for drawer resolution — campaign_id/campaign_key, never display name alone.
  const keyAttr = c.campaign_key != null ? ` data-campaign-key="${escapeHtml(String(c.campaign_key))}"` : "";
  const aliasHint = (c.aliases && c.aliases.length)
    ? `<span class="campaign-alias-hint" title="Also: ${escapeHtml(c.aliases.join(", "))}">+${c.aliases.length} alias${c.aliases.length === 1 ? "" : "es"}</span>` : "";
  const mapNote = c.mapping_status === "unmatched"
    ? `<span class="campaign-map-note" title="Lead label with no canonical Google Ads campaign mapping">unmapped</span>` : "";
  return `
    <tr class="campaign-decision-row" tabindex="0" role="button" data-campaign-open="${nameEnc}"${keyAttr}
        aria-label="Open evidence for ${escapeHtml(c.campaign_name || "campaign")}">
      <td class="td--name" data-label="Campaign">${escapeHtml(c.campaign_name || "—")}${aliasHint}${mapNote}</td>
      <td data-label="Status">${campaignStatusBadge(c.outcome_status)}</td>
      <td class="td--num" data-label="Spend">${campaignSpendCell(c)}</td>
      <td class="td--num" data-label="Leads">${leads}</td>
      <td class="td--num" data-label="SQLs">${sqls}</td>
      <td class="td--num" data-label="Junk">${junk}</td>
      <td class="td--num ${junkCls}" data-label="Junk Rate">${junkRateStr}</td>
      <td class="td--num ${cpql === "N/A" ? "td--na" : ""}" data-label="CPQL">${cpql}</td>
      <td class="td--action" data-label="">
        <span class="campaign-open-icon" aria-hidden="true" title="Open evidence">›</span>
      </td>
    </tr>`;
}

function wireCampaignEvidenceControls() {
  const search = document.getElementById("campaign-search");
  if (search) {
    search.addEventListener("input", (e) => { _campaignFilters.search = e.target.value; renderCampaignDecisionTable(); });
  }
  const status = document.getElementById("campaign-status");
  if (status) { status.addEventListener("change", (e) => { _campaignFilters.status = e.target.value; renderCampaignDecisionTable(); }); }
  const outcome = document.getElementById("campaign-outcome");
  if (outcome) { outcome.addEventListener("change", (e) => { _campaignFilters.outcome = e.target.value; renderCampaignDecisionTable(); }); }
  const sort = document.getElementById("campaign-sort");
  if (sort) { sort.addEventListener("change", (e) => { _campaignFilters.sort = e.target.value; renderCampaignDecisionTable(); }); }
  const clear = document.getElementById("campaign-clear-filters");
  if (clear) clear.addEventListener("click", () => {
    _campaignFilters.search = ""; _campaignFilters.status = "all";
    _campaignFilters.outcome = "all"; _campaignFilters.sort = "spend";
    renderCampaignEvidencePage();
  });
}

// ── Waste Terms page ───────────────────────────────────────────────────────

let _wasteData = [];  // raw API response, reset on each load
// PR-ADS-141: truncation metadata for the waste library ({total_count,
// returned_count, truncated}) so the UI can disclose "Showing X of Y".
let _wasteMeta = { total_count: 0, returned_count: 0, truncated: false };

const JUNK_CATEGORY_LABELS = {
  job_seeker:           "Job Seeker",
  student:              "Student",
  free_intent_english:  "Free Intent",
  free_intent_spanish:  "Free Intent ES",
  free_intent_arabic:   "Free Intent AR",
  fraud:                "Fraud",
};

function formatJunkCategory(cat) {
  if (!cat) return "—";
  if (JUNK_CATEGORY_LABELS[cat]) return JUNK_CATEGORY_LABELS[cat];
  // fallback: replace underscores with spaces and title-case
  return cat.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function junkCategoryBadge(cat) {
  const label = formatJunkCategory(cat);
  const slug  = (cat || "unknown").replace(/[^a-z0-9_-]/gi, "-").toLowerCase();
  return `<span class="junk-badge junk-badge--${escapeHtml(slug)}">${escapeHtml(label)}</span>`;
}

async function loadWaste() {
  renderPageDatasetFreshness("waste");

  const tableEl = document.getElementById("waste-table-body");
  if (tableEl) tableEl.innerHTML =
    `<p class="empty-state" style="padding:var(--space-5)">Loading waste terms…</p>`;

  ["waste-kpi-spend", "waste-kpi-terms", "waste-kpi-campaigns", "waste-kpi-crm"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.textContent = "—";
  });

  try {
    const data = await fetchJSON(`/api/waste?${evidenceWindowQuery()}`);
    _wasteData = data.waste || [];
    // PR-ADS-141: disclose truncation — the row list is a presentation-capped
    // page, so All time never silently implies the complete waste library.
    _wasteMeta = {
      total_count:    Number(data.total_count != null ? data.total_count : _wasteData.length),
      returned_count: Number(data.returned_count != null ? data.returned_count : _wasteData.length),
      truncated:      data.truncated === true || data.has_more === true,
    };

    populateWasteFilters(_wasteData);
    // PR-ADS-142: apply a campaign prefill from the campaign drawer, if present.
    if (wastePrefill && wastePrefill.campaign) {
      const sel = document.getElementById("waste-filter-campaign");
      if (sel && Array.from(sel.options).some((o) => o.value === wastePrefill.campaign)) {
        sel.value = wastePrefill.campaign;
      }
      wastePrefill = null;
    }
    renderWasteKPIs(_wasteData);
    applyWasteFilters();

  } catch (_) {
    _wasteData = [];
    _wasteMeta = { total_count: 0, returned_count: 0, truncated: false };
    if (tableEl) tableEl.innerHTML =
      `<p class="empty-state" style="padding:var(--space-5)">Could not load waste terms. Check API health or run status.</p>`;
  }
}

function renderWasteKPIs(items) {
  const totalSpend    = items.reduce((sum, t) => sum + (t.spend_usd || 0), 0);
  const uniqueTerms   = new Set(
    items.map((t) => (t.search_term || "").trim()).filter(Boolean)
  ).size;
  const uniqueCamps   = new Set(items.map((t) => t.campaign_name).filter(Boolean)).size;
  const crmConfirmed  = items.reduce((sum, t) => sum + (t.crm_junk_confirmed || 0), 0);

  const spendEl = document.getElementById("waste-kpi-spend");
  const termsEl = document.getElementById("waste-kpi-terms");
  const campsEl = document.getElementById("waste-kpi-campaigns");
  const crmEl   = document.getElementById("waste-kpi-crm");

  if (spendEl) spendEl.textContent = fmtDollar(totalSpend);
  if (termsEl) termsEl.textContent = String(uniqueTerms);
  if (campsEl) campsEl.textContent = String(uniqueCamps);
  if (crmEl)   crmEl.textContent   = String(crmConfirmed);
}

function populateWasteFilters(items) {
  const catSel  = document.getElementById("waste-filter-category");
  const campSel = document.getElementById("waste-filter-campaign");

  if (catSel) {
    const cats = [...new Set(items.map((t) => t.junk_category).filter(Boolean))].sort();
    catSel.innerHTML = `<option value="">All categories</option>` +
      cats.map((c) =>
        `<option value="${escapeHtml(c)}">${escapeHtml(formatJunkCategory(c))}</option>`
      ).join("");
  }

  if (campSel) {
    const camps = [...new Set(items.map((t) => t.campaign_name).filter(Boolean))].sort();
    campSel.innerHTML = `<option value="">All campaigns</option>` +
      camps.map((c) =>
        `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`
      ).join("");
  }
}

// Threshold above which a spend cell gets the high-spend style
const WASTE_HIGH_SPEND_USD = 100;

function renderWasteTable(items) {
  const tableEl = document.getElementById("waste-table-body");
  if (!tableEl) return;

  if (items.length === 0) {
    if (_wasteData.length === 0) {
      tableEl.innerHTML = buildEmptyState({
        pageKey: "waste",
        canonicalStatus: getPageCanonicalStatus("waste"),
        rowsInWindow: 0,
      });
    } else {
      tableEl.innerHTML = buildEmptyState({
        pageKey: "waste",
        filtersActive: true,
      });
    }
    return;
  }

  const thead = `
    <thead>
      <tr>
        <th>Search Term</th>
        <th>Campaign</th>
        <th class="td--num">Spend</th>
        <th>Junk Category</th>
        <th>Matched Pattern</th>
        <th class="td--num">CRM Confirmed</th>
        <th>Run Date</th>
      </tr>
    </thead>`;

  // PR-ADS-141: disclose when the window holds more terms than were loaded.
  const truncationNote = _wasteMeta.truncated
    ? `<p class="deal-stage-note">Showing ${escapeHtml(fmtCount(_wasteMeta.returned_count))} of `
      + `${escapeHtml(fmtCount(_wasteMeta.total_count))} waste terms in this window `
      + `(highest spend first). Narrow the window for the complete library.</p>`
    : "";

  const tbody = items.map((t) => {
    const highSpend = (t.spend_usd || 0) >= WASTE_HIGH_SPEND_USD;
    return `
      <tr${highSpend ? ' class="row--high-spend"' : ""}>
        <td class="td--name">${escapeHtml(t.search_term || "—")}</td>
        <td>${escapeHtml(t.campaign_name || "—")}</td>
        <td class="td--num${highSpend ? " waste-spend--high" : ""}">${t.spend_usd != null ? fmtDollar(t.spend_usd) : "—"}</td>
        <td>${t.junk_category ? junkCategoryBadge(t.junk_category) : "—"}</td>
        <td class="waste-pattern">${escapeHtml(t.matched_pattern || "—")}</td>
        <td class="td--num">${t.crm_junk_confirmed != null ? String(t.crm_junk_confirmed) : "—"}</td>
        <td>${fmtDate(t.run_date)}</td>
      </tr>`;
  }).join("");

  tableEl.innerHTML = truncationNote + `<table class="data-table">${thead}<tbody>${tbody}</tbody></table>`;
}

// Returns the currently visible (filtered) waste terms based on filter control state.
function getFilteredWasteTerms() {
  const searchInput = document.getElementById("waste-filter-search");
  const catSel      = document.getElementById("waste-filter-category");
  const campSel     = document.getElementById("waste-filter-campaign");

  const search = searchInput ? searchInput.value.trim().toLowerCase() : "";
  const cat    = catSel      ? catSel.value  : "";
  const camp   = campSel     ? campSel.value : "";

  let filtered = _wasteData;
  if (search) {
    filtered = filtered.filter((t) =>
      (t.search_term     || "").toLowerCase().includes(search) ||
      (t.campaign_name   || "").toLowerCase().includes(search) ||
      (t.matched_pattern || "").toLowerCase().includes(search)
    );
  }
  if (cat)  filtered = filtered.filter((t) => t.junk_category === cat);
  if (camp) filtered = filtered.filter((t) => t.campaign_name === camp);
  return filtered;
}

function applyWasteFilters() {
  renderWasteTable(getFilteredWasteTerms());
}

function copyWasteTerms() {
  const terms = getFilteredWasteTerms()
    .map((t) => (t.search_term || "").trim())
    .filter(Boolean);
  if (terms.length === 0) return;

  const text   = terms.join("\n");
  const btn    = document.getElementById("waste-copy-btn");
  const origHTML = btn ? btn.innerHTML : null;

  const showFeedback = (success) => {
    if (!btn) return;
    btn.innerHTML = success
      ? `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg> Copied!`
      : `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg> Copy failed`;
    btn.disabled = true;
    setTimeout(() => {
      if (btn && origHTML !== null) { btn.innerHTML = origHTML; btn.disabled = false; }
    }, 2000);
  };

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(
      () => showFeedback(true),
      () => showFeedback(false),
    );
  } else {
    // Fallback for older browsers
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity  = "0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) { /* ignore */ }
    document.body.removeChild(ta);
    showFeedback(ok);
  }
}

// ── Lead Quality page ──────────────────────────────────────────────────────

async function loadLeads() {
  renderPageDatasetFreshness("lead_quality");
  const tableEl    = document.getElementById("leads-table-body");
  const totalEl    = document.getElementById("leads-total");
  const sqlsEl     = document.getElementById("leads-sqls");
  const junkEl     = document.getElementById("leads-junk");
  const progressEl = document.getElementById("leads-progress");

  if (tableEl) tableEl.innerHTML =
    `<p class="empty-state" style="padding:var(--space-5)">Loading lead quality data…</p>`;

  try {
    const data  = await fetchJSON(`/api/leads?${evidenceWindowQuery()}`);
    // PR-ADS-141: KPIs and the per-campaign breakdown come from COMPLETE
    // server-side aggregates over the whole evidence window (deduped by contact),
    // never a truncated row list — so All time is never silently incomplete.
    const agg        = (data && data.aggregates) || { totals: {}, by_campaign: [] };
    const totals     = agg.totals || {};
    const byCampaign = agg.by_campaign || [];
    const totalLeads = Number(totals.total || 0);

    if (totalLeads === 0) {
      if (tableEl) tableEl.innerHTML = buildEmptyState({
        pageKey: "leads",
        canonicalStatus: getPageCanonicalStatus("leads"),
        rowsInWindow: 0,
      });
      [totalEl, sqlsEl, junkEl, progressEl].forEach((el) => {
        if (el) el.textContent = "—";
      });
      return;
    }

    if (totalEl)    totalEl.textContent    = String(totalLeads);
    if (sqlsEl)     sqlsEl.textContent     = String(Number(totals.qualified || 0));
    if (junkEl)     junkEl.textContent     = String(Number(totals.junk || 0));
    if (progressEl) progressEl.textContent = String(Number(totals.in_progress || 0));

    const thead = `
      <thead>
        <tr>
          <th>Campaign</th>
          <th class="td--num">Total</th>
          <th class="td--num">SQL</th>
          <th class="td--num">In Progress</th>
          <th class="td--num">Junk</th>
          <th class="td--num">Wrong Fit</th>
          <th class="td--num">Unknown</th>
          <th>Junk Rate</th>
        </tr>
      </thead>`;

    const tbody = byCampaign.map((g) => {
      const total     = Number(g.total || 0);
      const sql       = Number(g.qualified || 0);
      const progress  = Number(g.in_progress || 0);
      const junk      = Number(g.junk || 0);
      const wrongFit  = Number(g.wrong_fit || 0);
      const unknown   = Number(g.unknown || 0);
      const junkPct = total > 0 ? Math.round((junk / total) * 100) : 0;
      const barCls  = junkPct < uiThresholds.junk_rate.low_pct   ? "progress-bar__fill--low" :
                      junkPct <= uiThresholds.junk_rate.high_pct ? "progress-bar__fill--mid" : "progress-bar__fill--high";
      const junkCls = junkPct < uiThresholds.junk_rate.low_pct   ? "junk--low" :
                      junkPct <= uiThresholds.junk_rate.high_pct ? "junk--mid" : "junk--high";
      return `
        <tr>
          <td class="td--name">${escapeHtml(g.campaign_name || "(unknown)")}</td>
          <td class="td--num">${total}</td>
          <td class="td--num">${sql}</td>
          <td class="td--num">${progress}</td>
          <td class="td--num">${junk}</td>
          <td class="td--num">${wrongFit}</td>
          <td class="td--num">${unknown}</td>
          <td>
            <div style="display:flex;align-items:center;gap:8px;">
              <div class="progress-bar" style="width:80px">
                <div class="progress-bar__fill ${barCls}" style="width:${junkPct}%"></div>
              </div>
              <span class="${junkCls}" style="font-size:12px;font-weight:500;">${junkPct}%</span>
            </div>
          </td>
        </tr>`;
    }).join("");

    if (tableEl) tableEl.innerHTML =
      `<table class="data-table">${thead}<tbody>${tbody}</tbody></table>`;

  } catch (_) {
    if (tableEl) tableEl.innerHTML =
      `<p class="empty-state" style="padding:var(--space-5)">Could not load lead quality data.</p>`;
  }
}

// ── Deals page ─────────────────────────────────────────────────────────────

// ── Closed-Won Deals — revenue ledger (PR-ADS-113) ──────────────────────────
// Deal-level revenue truth: HubSpot closed-won deals attributed to Google Ads,
// windowed by deal_close_date (NEVER the scheduler run_date). This is revenue
// truth, not an active pipeline funnel (active pipeline lives in In Progress
// Leads). Two distinct unhappy states: the durable ledger is unavailable, vs a
// safe-empty window with no closed-won deals.

let revenueDealsData = [];
let revenueDealsSummary = null;
let revenueDealsLedgerStatus = null;

function dealAttributionBadge(matchSource) {
  switch ((matchSource || "").trim().toLowerCase()) {
    case "gclid":     return '<span class="attribution-badge attribution-badge--tier1" title="Click-level match using GCLID evidence">Exact GCLID</span>';
    case "crm_field": return '<span class="attribution-badge attribution-badge--tier2" title="Matched using a HubSpot CRM campaign field">CRM Match</span>';
    case "first_url": return '<span class="attribution-badge attribution-badge--tier3" title="Matched using the landing-page URL">URL Match</span>';
    default:          return '<span class="attribution-badge" title="No reliable attribution evidence — review needed">Needs Review</span>';
  }
}

function renderRevenueDealsUnavailable() {
  return `
    <div class="revenue-blocked-card">
      <div class="revenue-blocked-card__eyebrow">Revenue ledger</div>
      <h3>Closed-won revenue is unavailable</h3>
      <p>The durable attribution ledger cannot be read right now.</p>
    </div>`;
}

function renderRevenueDealsSafeEmptyState() {
  return `
    <div class="revenue-blocked-card revenue-empty-card">
      <div class="revenue-blocked-card__eyebrow">Revenue ledger</div>
      <h3>No closed-won deals found for this window</h3>
      <p>The attribution ledger is available, but no HubSpot closed-won deals closed in this business window.</p>
    </div>`;
}

function renderRevenueDealsSummary(summary) {
  const s = summary || {};
  return `
    <div class="revenue-summary-strip revenue-summary-strip--four">
      <div><span>Closed-Won Deals</span><strong>${fmtCount(s.deal_count)}</strong></div>
      <div><span>Won Revenue</span><strong>${fmtMoney(s.won_revenue)}</strong></div>
      <div><span>Average Deal Value</span><strong>${fmtMoney(s.average_deal_value)}</strong></div>
      <div><span>Exact GCLID Matches</span><strong>${fmtCount(s.exact_gclid_count)}</strong></div>
    </div>
  `;
}

// Sort: most recent close first, then largest revenue.
function sortRevenueDealRows(rows) {
  return [...rows].sort((a, b) =>
    String(b.deal_close_date || "").localeCompare(String(a.deal_close_date || "")) ||
    (b.deal_amount_usd || 0) - (a.deal_amount_usd || 0)
  );
}

function renderRevenueDealsTable(rows) {
  const headerRow = `
    <tr>
      <th>Company</th>
      <th>Closed</th>
      <th>Country</th>
      <th>Campaign</th>
      <th>Won Revenue</th>
      <th>Attribution</th>
    </tr>
  `;
  const bodyRows = rows.map((r) => `
    <tr>
      <td>${escapeHtml(r.company || "—")}</td>
      <td>${escapeHtml(r.deal_close_date || "—")}</td>
      <td>${escapeHtml(r.country || "—")}</td>
      <td>${escapeHtml(r.campaign_name || "—")}</td>
      <td>${fmtMoney(r.deal_amount_usd)}</td>
      <td>${dealAttributionBadge(r.match_source)}</td>
    </tr>
  `).join("");
  return `
    <div class="table-scroll">
      <table class="data-table roas-table revenue-decision-table">
        ${headerRow}
        <tbody>${bodyRows}</tbody>
      </table>
    </div>
  `;
}

function renderRevenueDealsPage() {
  const kpiGrid = document.getElementById("revenue-deals-kpis");
  const tableBody = document.getElementById("revenue-deals-table-body");
  if (kpiGrid) kpiGrid.innerHTML = "";
  if (!tableBody) return;

  // Mart endpoint failed — NO silent fallback to /api/revenue-deals.
  if (revenueDealsLedgerStatus === "mart_unavailable") {
    tableBody.innerHTML = renderMartUnavailable();
    return;
  }

  // Unavailable: the durable ledger/database cannot be read.
  if (revenueDealsLedgerStatus === "database_unavailable") {
    tableBody.innerHTML = renderRevenueDealsUnavailable();
    return;
  }

  // Safe-empty: ledger available but no closed-won deals in this window.
  if (!revenueDealsData.length) {
    tableBody.innerHTML = renderRevenueDealsSafeEmptyState();
    return;
  }

  const sorted = sortRevenueDealRows(revenueDealsData);
  tableBody.innerHTML = `
    ${renderMartSpendTruth(revenueDealsMart)}
    ${renderRevenueDealsSummary(martDealLedgerSummary(revenueDealsMart))}
    ${renderMartDiagnostics(revenueDealsMart)}
    ${renderRevenueDealsTable(sorted)}
    <p class="revenue-footnote">Canonical Revenue Decision Mart — HubSpot closed-won deals provide revenue, attributed to Google Ads by deal close date.</p>
  `;
}

async function loadDeals() {
  const window_ = getRoasBusinessWindow();
  const token = ++_revReqSeq.deals;
  setWindowRangeLoading("revenue-deals-range");
  const kpiGrid = document.getElementById("revenue-deals-kpis");
  const tableBody = document.getElementById("revenue-deals-table-body");
  if (kpiGrid) kpiGrid.innerHTML = "";
  if (tableBody) tableBody.innerHTML = '<p class="empty-state" style="padding:var(--space-5)">Loading closed-won deals…</p>';

  try {
    // PR-ADS-126: PRIMARY truth is the canonical mart deal view.
    const data = await fetchRevenuePerformance("deal", window_);
    if (token !== _revReqSeq.deals) return;  // stale response superseded
    if (!data) {
      // Mart endpoint failed — NO silent fallback to /api/revenue-deals.
      revenueDealsMart = null;
      revenueDealsLedgerStatus = "mart_unavailable";
      revenueDealsData = [];
      revenueDealsSummary = null;
      renderRevenueDealsPage();
      return;
    }
    if (!_revResponseIsCurrent("deals", token, data)) return;
    revenueDealsMart = data;
    // PR-ADS-127: a durable deal-ledger OUTAGE must never read as an empty
    // window. The mart surfaces the ledger's own availability in data_health; a
    // "database_unavailable" status renders the honest "ledger cannot be read"
    // card, not "No closed-won deals found".
    const dealLedgerStatus = (data.data_health && data.data_health.deal_ledger_status) || "available";
    revenueDealsLedgerStatus = dealLedgerStatus === "database_unavailable"
      ? "database_unavailable" : "available";
    // PR-ADS-127: the Deals summary comes from the ledger's OWN summary so its
    // count / Won Revenue / average all reconcile with the displayed deal rows.
    revenueDealsSummary = data.ledger_summary || null;
    revenueDealsData = data.rows || [];
    renderWindowRange("revenue-deals-range", data.window || null);
    renderRevenueDealsPage();
  } catch (err) {
    console.error("[loadDeals]", err);
    if (token !== _revReqSeq.deals) return;
    revenueDealsMart = null;
    revenueDealsLedgerStatus = "mart_unavailable";
    revenueDealsData = [];
    revenueDealsSummary = null;
    renderRevenueDealsPage();
  }
}

// Deal-ledger summary for the summary strip. PR-ADS-127: the count, Won Revenue,
// average and exact-GCLID count ALL come from the mart's deal-ledger summary —
// the SAME source as the displayed deal rows — so the strip always reconciles
// with the table. (Earlier this mixed the canonical attribution total with the
// raw ledger count, which diverged whenever a "not Google Ads" exclusion or an
// identity remap was applied.) Falls back to aggregating the rows only when the
// backend omits the ledger summary (older payloads).
function martDealLedgerSummary(data) {
  if (data && data.ledger_summary) return data.ledger_summary;
  const rows = (data && data.rows) || [];
  const dealCount = rows.length;
  const wonRevenue = rows.reduce((acc, r) => acc + (Number(r.deal_amount_usd) || 0), 0);
  return {
    deal_count: dealCount,
    won_revenue: dealCount ? Math.round(wonRevenue * 100) / 100 : null,
    average_deal_value: dealCount ? Math.round((wonRevenue / dealCount) * 100) / 100 : null,
    exact_gclid_count: rows.filter((r) => (r.match_source || "").toLowerCase() === "gclid").length,
  };
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

  const leadGrainOk = !!dg.lead_window_safe;
  // PR-ADS-115 split contract: integration connected vs selected-window status.
  const integrationConnected = (audit.revenue_integration_status
    ? audit.revenue_integration_status === "connected"
    : audit.revenue_attribution_wired === true);
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

  // Revenue truth is ready when the audit is SAFE, lead dates are safe, and the
  // integration is connected. A connected integration whose selected window has
  // no closed-won deals is a SAFE EMPTY state — never "not wired".
  const revenueReady = audit.verdict === "SAFE" && leadGrainOk && integrationConnected;

  const blockers = (audit.blockers || []).slice();
  if (!integrationConnected) {
    blockers.push("Revenue integration is not connected: no attributed closed-won deals exist yet.");
  }

  // The next action points at a REAL admin workflow — never a nonexistent
  // generic lead re-import action.
  let nextAction = "Revenue truth is ready — no action needed.";
  if (!leadGrainOk) {
    nextAction = "Run Lead Event-Date Reconciliation (below) to resolve included paid leads still missing an event date.";
  } else if (!integrationConnected) {
    nextAction = "Run Revenue Truth Recovery (below) to connect closed-won attribution, then re-check this page.";
  } else if (!pollutionOk) {
    nextAction = "Clean pseudo/email campaign rows from the leads table, then re-check this page.";
  }

  const readinessCard = revenueReady
    ? `<div class="revenue-blocked-card revenue-empty-card">
         <div class="revenue-blocked-card__eyebrow">Revenue readiness</div>
         <h3>Revenue Ready</h3>
         <p>The audit is SAFE, lead event dates are safe, and the revenue integration is connected.${!windowHasRevenue ? " The selected window has no closed-won deals yet — a safe empty state." : ""}</p>
       </div>`
    : `<div class="revenue-blocked-card">
         <div class="revenue-blocked-card__eyebrow">Revenue readiness</div>
         <h3>Revenue is not ready</h3>
         <p>Revenue truth is ready only when the audit is SAFE, lead event dates are safe, and the revenue integration is connected. (A connected integration with no closed-won deals in the selected window is safe-empty, not a blocker.)</p>
         <ul class="revenue-health-blockers">${blockers.map((b) => `<li>${escapeHtml(b)}</li>`).join("")}</ul>
         <div class="revenue-next-step"><strong>Next step:</strong> ${escapeHtml(nextAction)}</div>
       </div>`;

  const fmtRange = (r) => {
    if (!r || (!r.start_date && !r.end_date)) return "—";
    return `${r.start_date || "Earliest"} → ${r.end_date || "—"}`;
  };

  const cmpRow = (k, label) => {
    const s = wc[k] || {};
    const leads = (s.leads === null || s.leads === undefined) ? "Withheld" : fmtCount(s.leads);
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
      ${revenueHealthCheckChip("Lead date grain", leadGrainOk, "Event-date safe", "Unsafe (sync date)")}
      ${revenueHealthCheckChip("Revenue integration", integrationConnected, "Connected", "Not connected")}
      ${revenueHealthCheckChip("Selected window", true, windowHasRevenue ? "Revenue rows" : "No closed-won revenue (safe)", "")}
      ${revenueHealthCheckChip("Campaign pollution", pollutionOk, "Clean", `${pollutionCount} polluted row(s)`)}
      ${revenueHealthCheckChip("Window integrity", windowIntegrityOk, "Windows differ", "Windows identical")}
    </div>

    ${readinessCard}

    <div class="revenue-health-rails">
      <h3>Source date rails</h3>
      <ul>
        <li><span>Spend</span><code>${escapeHtml(dg.spend_date_field || "geo.run_date")}</code></li>
        <li><span>Leads</span><code>${escapeHtml(dg.lead_date_field_current || "leads.contact_created_at")}</code></li>
        <li><span>Revenue</span><code>${escapeHtml(dg.deal_date_field || "gclid_attribution.deal_close_date")}</code></li>
      </ul>
    </div>

    <div class="revenue-summary-strip revenue-summary-strip--four">
      <div><span>Included paid leads missing dates</span><strong>${fmtCount(audit.missing_contact_created_at_count)}</strong></div>
      <div><span>Legacy paid rows excluded from revenue truth</span><strong>${fmtCount(audit.legacy_excluded_count)}</strong></div>
      <div><span>Excluded non-paid rows</span><strong>${fmtCount(audit.excluded_non_paid_count)}</strong></div>
      <div><span>Excluded pseudo campaigns</span><strong>${fmtCount(audit.excluded_pseudo_campaign_count)}</strong></div>
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
          <div class="revenue-parity-summary__verdict">All revenue pages agree with mart: <strong>${allAgree ? "Yes" : "No"}</strong></div>
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
        ${rows.map(([label, val]) => `<div><span>${label}</span><strong>${fmtCount(val)}</strong></div>`).join("")}
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
    case "inspect_geo_query_parity": return "Inspect geo/campaign query parity — running Geo Sync again will not close this gap. Country ROAS stays blocked until the geo source can reconcile.";
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
        <div><span>Unattributed / No Country residual</span><strong>${money(r.country_residual_native)}${pct}</strong></div>
      </div>
      <p class="revenue-geo-cause">Country ROAS is shown with a residual row. Real country rows use country-attributed spend only. Running Geo Sync again will not close this gap — the geographic view does not assign this spend to a country by design.</p>
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
        <p class="revenue-footnote">Country ROAS needs geo-table spend to reconcile with canonical campaign spend. Runs in the background. Reads Google Ads read-only (geographic_view) and writes only the local database; never writes to Google Ads or HubSpot.</p>
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
    ["Already excluded legacy rows", s.already_excluded_legacy],
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
        ${rows.map(([label, val]) => `<div><span>${label}</span><strong>${fmtCount(val)}</strong></div>`).join("")}
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

// ── Revenue by Acquisition Source (PR-ADS-117) ──────────────────────────────
// Google Ads is the only group with connected spend, so the only group that may
// show ROAS. Every other group is revenue-only (ROAS unavailable, never $0/0x).

// ── PR-ADS-133: Revenue by Source — group → channel → platform taxonomy ──────
// Each top-level group renders a section with its channels; each channel lists
// its platform rows, and every platform row expands into a drawer proving the
// clients/deals behind it. Only Google Ads is spend-connected, so only Google
// Ads shows ROAS — every other row is revenue-only with an honest status label
// (never a fabricated $0 / 0.00x). Missing values render "Unavailable".

// SQL / Customer conversion rate off the lead base; Unavailable when no leads.
function fmtSourceRate(numer, denom) {
  if (!denom || denom <= 0 || numer === null || numer === undefined) return "Unavailable";
  return (100 * numer / denom).toFixed(0) + "%";
}

// Group-level ROAS / status cell: Google shows ROAS; others show why not.
function sourceStatusLabel(group) {
  if (group.has_spend === true) {
    if (group.roas !== null && group.roas !== undefined) return fmtRoasMultiple(group.roas);
    // PR-ADS-140: when canonical Google Ads spend/ROAS is withheld, show the exact
    // honest reason (FX withheld / coverage incomplete / source unavailable) —
    // never a bare "Unavailable" and never "no connected spend source".
    return group.spend_status_label || "Unavailable";
  }
  if (group.group === "offline") return "Imported CRM records — no reliable source attribution";
  if (group.group === "unclassified") return "Needs review — source missing or unsafe";
  return "Revenue-only — no connected spend source";
}

// PR-ADS-140: compact proof chip inside the Google Ads section — the Revenue by
// Source Google Ads spend is the canonical campaign spend, NOT country/geo spend.
function renderSourceSpendProofChip(spendTruth) {
  const st = spendTruth || {};
  const tip = st.geo_spend_note
    || "Country geo spend is diagnostic and is not used as the Revenue by Source denominator.";
  return `<span class="source-proof-chip" title="${escapeHtml(tip)}">Spend source: canonical campaign spend</span>`;
}

// Expand control for a platform row (opens the clients/deals drawer, lazy).
function sourcePlatformExpandControl(idx, group, channel, platform, label) {
  const aria = `Show clients / deals behind ${label || "this platform"}`;
  return `<button type="button" class="source-expand-button" data-source-expand data-source-idx="${idx}"`
    + ` data-source-group="${encodeURIComponent(group)}" data-source-channel="${encodeURIComponent(channel)}"`
    + ` data-source-platform="${encodeURIComponent(platform)}" aria-expanded="false"`
    + ` aria-label="${escapeHtml(aria)}" title="${escapeHtml(aria)}">▸</button>`;
}

function renderSourcePlatformRow(group, ch, pf, idx) {
  // ROAS shows ONLY for a spend-connected platform (the canonical Google Ads
  // bucket). fmtRoasMultiple already renders a null ROAS as "Unavailable", so a
  // spend-connected-but-zero bucket never shows NaN; every other bucket shows its
  // honest status string, never a fabricated ROAS. (group.has_spend gates the
  // group; pf.spend gates the individual bucket — see PR-133 review.)
  const hasSpend = pf.spend !== null && pf.spend !== undefined;
  const statusCell = hasSpend ? fmtRoasMultiple(pf.roas) : `<span class="source-status">${escapeHtml(pf.status || "")}</span>`;
  const spendCell = hasSpend ? fmtMoney(pf.spend) : '<span class="detail-unavailable">Unavailable</span>';
  return `
    <tr class="source-platform-row">
      <td class="source-platform-name">${sourcePlatformExpandControl(idx, group.group, ch.channel, pf.platform, pf.label)}${escapeHtml(pf.label)}</td>
      <td>${fmtCount(pf.leads)}</td>
      <td>${fmtCount(pf.sqls)}</td>
      <td>${fmtSourceRate(pf.sqls, pf.leads)}</td>
      <td>${fmtCount(pf.customers)}</td>
      <td>${fmtSourceRate(pf.customers, pf.leads)}</td>
      <td>${fmtMoney(pf.won_revenue)}</td>
      <td>${spendCell}</td>
      <td>${statusCell}</td>
    </tr>
    <tr class="source-drilldown-row" data-source-drilldown-idx="${idx}" hidden>
      <td colspan="9"><div class="source-drilldown-panel"></div></td>
    </tr>`;
}

function renderSourceChannelRows(group, ch, gi, ci) {
  // Only the spend-connected (canonical Paid Search) channel carries spend/ROAS;
  // gate on ch.spend so a non-canonical channel in the Google group never shows it.
  const hasSpend = ch.spend !== null && ch.spend !== undefined;
  const spendCell = hasSpend ? fmtMoney(ch.spend) : '<span class="detail-unavailable">Unavailable</span>';
  const statusCell = hasSpend ? fmtRoasMultiple(ch.roas) : "—";
  const channelHead = `
    <tr class="source-channel-row">
      <td class="source-channel-name">${escapeHtml(ch.label)}</td>
      <td>${fmtCount(ch.leads)}</td>
      <td>${fmtCount(ch.sqls)}</td>
      <td>${fmtSourceRate(ch.sqls, ch.leads)}</td>
      <td>${fmtCount(ch.customers)}</td>
      <td>${fmtSourceRate(ch.customers, ch.leads)}</td>
      <td>${fmtMoney(ch.won_revenue)}</td>
      <td>${spendCell}</td>
      <td>${statusCell}</td>
    </tr>`;
  const platformRows = (ch.platforms || [])
    .map((pf, pi) => renderSourcePlatformRow(group, ch, pf, `${gi}-${ci}-${pi}`))
    .join("");
  return channelHead + platformRows;
}

function renderSourceGroupSection(group, gi, spendTruth) {
  const channels = group.channels || [];
  const headerRow = `
    <tr>
      <th>Channel / Platform</th>
      <th>Leads</th>
      <th>SQLs</th>
      <th>SQL Rate</th>
      <th>Customers</th>
      <th>Customer Rate</th>
      <th>Won Revenue</th>
      <th>Spend</th>
      <th>ROAS / Status</th>
    </tr>`;
  const body = channels.length
    ? channels.map((ch, ci) => renderSourceChannelRows(group, ch, gi, ci)).join("")
    : `<tr><td colspan="9" class="source-drilldown-empty">No attributed activity in this window.</td></tr>`;
  // Group KPI strip — Google shows Spend/ROAS; others show the revenue-only reason.
  const isGoogle = group.has_spend === true;
  // PR-ADS-140: the Google Ads spend chip is the CANONICAL campaign spend (the
  // same USD number shown at the top), with the native GBP evidence as a subline —
  // never the old geo/country-derived figure. Unavailable (never $0) when withheld.
  const st = spendTruth || {};
  const nativeSub = (isGoogle && st.native_spend !== null && st.native_spend !== undefined)
    ? `<small class="source-spend-native">${escapeHtml(fmtCompactCurrency(st.native_spend, st.native_currency))} native</small>`
    : "";
  const googleSpendText = group.spend === null || group.spend === undefined
    ? "Unavailable" : fmtMoney(group.spend);
  const spendChip = isGoogle
    ? `<div><span>Spend</span><strong>${googleSpendText}</strong>${nativeSub}</div>`
    : `<div><span>Spend</span><strong>Unavailable</strong></div>`;
  const proofChip = isGoogle ? renderSourceSpendProofChip(st) : "";
  return `
    <div class="panel revenue-source-section">
      <div class="panel__header source-group-header">
        <span class="source-group-title">${escapeHtml(group.label)}</span>
        <span class="source-group-status">${escapeHtml(sourceStatusLabel(group))}</span>
        ${proofChip}
      </div>
      <div class="revenue-summary-strip revenue-summary-strip--four source-group-kpis">
        <div><span>Leads</span><strong>${fmtCount(group.leads)}</strong></div>
        <div><span>Customers</span><strong>${fmtCount(group.customers)}</strong></div>
        <div><span>Won Revenue</span><strong>${fmtMoney(group.won_revenue)}</strong></div>
        ${spendChip}
      </div>
      <div class="panel__body panel__body--flush">
        <div class="revenue-table-shell">
          <div class="revenue-table-scroll">
            <table class="data-table roas-table revenue-decision-table source-taxonomy-table">
              ${headerRow}
              <tbody>${body}</tbody>
            </table>
          </div>
        </div>
      </div>
    </div>`;
}

// ── Source platform drawer (clients / deals behind a platform) ───────────────
let sourceDrilldownCache = {};

function sourceDrilldownKey(group, channel, platform) {
  return `${getRoasBusinessWindow()}:${group}:${channel}:${platform}`;
}

function renderSourceDrilldownRow(d) {
  const amount = d.amount == null ? drilldownValue(null) : fmtMoney(d.amount);
  return `
    <tr>
      <td>${drilldownValue(d.company)}</td>
      <td>${drilldownValue(d.company_id)}</td>
      <td>${drilldownValue(d.main_contact)}</td>
      <td>${drilldownValue(d.contact_id)}</td>
      <td>${drilldownValue(d.lifecycle_stage)}</td>
      <td>${drilldownValue(d.status_category)}</td>
      <td>${drilldownValue(d.deal)}</td>
      <td>${drilldownValue(d.deal_id)}</td>
      <td>${amount}</td>
      <td>${drilldownValue(d.close_date)}</td>
      <td>${drilldownValue(d.source)}</td>
      <td>${drilldownValue(d.source_drilldown_1)}</td>
      <td>${drilldownValue(d.source_drilldown_2)}</td>
      <td>${drilldownValue(d.campaign_source_label)}</td>
      <td>${drilldownValue(d.attribution_status)}</td>
    </tr>`;
}

// One contact (lead / SQL) proof row. Same safe formatter; unavailable for any
// field not durably stored (contact name, company id, lifecycle).
function renderSourceContactRow(c) {
  return `
    <tr>
      <td>${drilldownValue(c.company)}</td>
      <td>${drilldownValue(c.company_id)}</td>
      <td>${drilldownValue(c.main_contact)}</td>
      <td>${drilldownValue(c.contact_id)}</td>
      <td>${drilldownValue(c.lifecycle_stage)}</td>
      <td>${drilldownValue(c.status_category)}</td>
      <td>${c.is_sql ? "Yes" : "No"}</td>
      <td>${drilldownValue(c.created_date)}</td>
      <td>${drilldownValue(c.source)}</td>
      <td>${drilldownValue(c.source_drilldown_1)}</td>
      <td>${drilldownValue(c.source_drilldown_2)}</td>
      <td>${drilldownValue(c.campaign_source_label)}</td>
    </tr>`;
}

function renderSourceDrilldown(data) {
  const status = (data && data.source_health && data.source_health.status) || "ready";
  if (status === "database_unavailable") {
    return `<p class="source-drilldown-muted">Source client details could not be loaded.</p>`;
  }
  const contacts = (data && data.contacts) || [];
  const deals = (data && data.deals) || (data && data.rows) || [];
  if (!contacts.length && !deals.length) {
    return `<div class="source-drilldown-empty">No attributed closed-won client detail found for this source in this window.</div>`;
  }
  // Section 1 — Leads / SQLs proof (windowed by contact_created_at).
  const contactHeader = `
    <tr>
      <th>Company</th><th>Company ID</th><th>Main Contact</th><th>Contact ID</th>
      <th>Lifecycle</th><th>SQL / Customer Status</th><th>SQL</th><th>Created</th>
      <th>Source</th><th>Source Drilldown 1</th><th>Source Drilldown 2</th>
      <th>Campaign / Source</th>
    </tr>`;
  const contactsSection = contacts.length
    ? `
    <div class="source-drilldown-title">Leads / SQLs Behind This Source</div>
    <div class="table-scroll source-drilldown-scroll">
      <table class="data-table revenue-decision-table source-drilldown-table">
        ${contactHeader}
        <tbody>${contacts.map(renderSourceContactRow).join("")}</tbody>
      </table>
    </div>`
    : `<div class="source-drilldown-title">Leads / SQLs Behind This Source</div>
       <div class="source-drilldown-empty">No leads/SQLs found for this source in this window.</div>`;
  // Section 2 — Closed-Won Deals proof (windowed by deal_close_date).
  const dealHeader = `
    <tr>
      <th>Company</th><th>Company ID</th><th>Main Contact</th><th>Contact ID</th>
      <th>Lifecycle</th><th>SQL / Customer Status</th><th>Deal</th><th>Deal ID</th>
      <th>Amount</th><th>Close Date</th><th>Source</th><th>Source Drilldown 1</th>
      <th>Source Drilldown 2</th><th>Campaign / Source</th><th>Attribution</th>
    </tr>`;
  const dealsSection = deals.length
    ? `
    <div class="source-drilldown-title">Closed-Won Deals Behind This Source</div>
    <div class="table-scroll source-drilldown-scroll">
      <table class="data-table revenue-decision-table source-drilldown-table">
        ${dealHeader}
        <tbody>${deals.map(renderSourceDrilldownRow).join("")}</tbody>
      </table>
    </div>`
    : `<div class="source-drilldown-title">Closed-Won Deals Behind This Source</div>
       <div class="source-drilldown-empty">No closed-won deals found for this source in this window.</div>`;
  return `${contactsSection}${dealsSection}`;
}

async function loadSourceDrilldown(group, channel, platform, drawerRow) {
  const container = drawerRow.querySelector(".source-drilldown-panel");
  if (!container) return;
  const key = sourceDrilldownKey(group, channel, platform);
  if (sourceDrilldownCache[key]) {
    container.innerHTML = renderSourceDrilldown(sourceDrilldownCache[key]);
    return;
  }
  container.innerHTML = `<p class="source-drilldown-muted">Loading source client details…</p>`;
  try {
    const window_ = getRoasBusinessWindow();
    const res = await fetch(
      `/api/revenue-performance/source-platform-detail?window=${encodeURIComponent(window_)}`
      + `&source_group=${encodeURIComponent(group)}&source_channel=${encodeURIComponent(channel)}`
      + `&source_platform=${encodeURIComponent(platform)}`,
      { credentials: "same-origin" }
    );
    if (!res.ok) {
      container.innerHTML = `<p class="source-drilldown-muted">Source client details could not be loaded.</p>`;
      return;
    }
    const data = await res.json();
    sourceDrilldownCache[key] = data;
    container.innerHTML = renderSourceDrilldown(data);
  } catch (err) {
    console.error("[loadSourceDrilldown]", err);
    container.innerHTML = `<p class="source-drilldown-muted">Source client details could not be loaded.</p>`;
  }
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-source-expand]");
  if (!btn) return;
  const idx = btn.getAttribute("data-source-idx");
  const group = decodeURIComponent(btn.getAttribute("data-source-group") || "");
  const channel = decodeURIComponent(btn.getAttribute("data-source-channel") || "");
  const platform = decodeURIComponent(btn.getAttribute("data-source-platform") || "");
  const drawer = document.querySelector(`.source-drilldown-row[data-source-drilldown-idx="${idx}"]`);
  if (!drawer) return;
  if (drawer.hasAttribute("hidden")) {
    drawer.removeAttribute("hidden");
    btn.setAttribute("aria-expanded", "true");
    btn.textContent = "▾";
    if (!drawer.dataset.loaded) {
      drawer.dataset.loaded = "1";
      loadSourceDrilldown(group, channel, platform, drawer);
    }
  } else {
    drawer.setAttribute("hidden", "");
    btn.setAttribute("aria-expanded", "false");
    btn.textContent = "▸";
  }
});

function renderRevenueBySourceHealth(summary) {
  const s = summary || {};
  return `
    <div class="revenue-summary-strip revenue-summary-strip--four">
      <div><span>Contacts classified</span><strong>${fmtCount(s.contacts_classified)}</strong></div>
      <div><span>Deals attributed</span><strong>${fmtCount(s.deals_attributed)}</strong></div>
      <div><span>Ambiguous deals</span><strong>${fmtCount(s.ambiguous_deals)}</strong></div>
      <div><span>Unclassified deals</span><strong>${fmtCount(s.unclassified_deals)}</strong></div>
    </div>
  `;
}

function renderRevenueBySourcePage(data) {
  const body = document.getElementById("revenue-by-source-body");
  if (!body) return;
  // PR-ADS-126: rows ARE the per-source groups from the canonical mart. PR-ADS-133
  // enriches each group with a channel → platform breakdown; only the Google Ads
  // group carries spend/ROAS (the mart sets has_spend / roas), so the frontend
  // never decides which source has spend. Empty groups are dropped to keep the
  // page a clean attribution view, not a diagnostics wall.
  const groups = (data && data.rows) || [];
  // PR-ADS-140: the canonical Google Ads spend-truth proof block (native GBP + USD
  // reporting + FX/coverage status). Drives the Google Ads spend chip subline and
  // the "canonical campaign spend" proof chip so the page proves its number.
  const spendTruth = (data && data.source_spend_truth) || {};
  const active = groups.filter((g) => g && ((g.channels && g.channels.length) || g.leads || g.customers));
  const sections = active.length
    ? active.map((g, gi) => renderSourceGroupSection(g, gi, spendTruth)).join("")
    : `<div class="revenue-empty-inline">No attributed source activity in this window.</div>`;
  body.innerHTML = `
    ${renderMartSpendTruth(data)}
    ${renderMartDiagnostics(data)}
    ${sections}
    <p class="revenue-footnote">Canonical Revenue Decision Mart — Google Ads provides spend, so only Google Ads shows ROAS. Every other channel/platform is revenue-only; ROAS is never fabricated. Expand a platform to prove the clients/deals behind it.</p>
  `;
}

async function loadRevenueBySource() {
  const window_ = getRoasBusinessWindow();
  const token = ++_revReqSeq.bySource;
  setWindowRangeLoading("revenue-by-source-range");
  renderSourceBackfillPanelInit();
  const body = document.getElementById("revenue-by-source-body");
  if (body) body.innerHTML = '<p class="empty-state" style="padding:var(--space-5)">Loading revenue by source…</p>';
  try {
    // PR-ADS-126: PRIMARY truth is the canonical mart source view.
    const data = await fetchRevenuePerformance("source", window_);
    if (token !== _revReqSeq.bySource) return;  // stale response superseded
    if (!data) {
      if (body) body.innerHTML = renderMartUnavailable();
      return;
    }
    if (!_revResponseIsCurrent("bySource", token, data)) return;
    renderWindowRange("revenue-by-source-range", data.window || null);
    renderRevenueBySourcePage(data);
  } catch (err) {
    console.error("[loadRevenueBySource]", err);
    if (token !== _revReqSeq.bySource) return;
    if (body) body.innerHTML = renderMartUnavailable();
  }
}

// ── Source Attribution Backfill panel (admin-only) ──────────────────────────

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
  if (_currentPage !== "revenue-by-source") { stopSourceBackfillPolling(); return; }
  try {
    const status = await fetchSourceBackfillStatus();
    if (status) {
      if (status.job_id) sourceBackfillJobId = status.job_id;
      renderSourceBackfillPanel(status);
      if (status.running) {
        sourceBackfillPollTimer = setTimeout(pollSourceBackfillOnce, 2500);
        return;
      }
      loadRevenueBySource();
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
        ${(a.fx_missing_dates && a.fx_missing_dates.length) ? `<p class="revenue-footnote">FX coverage incomplete — missing rates for ${a.fx_missing_dates.length} date(s). USD ROAS is unavailable until FX coverage is complete.</p>` : ""}
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

  const metrics = `
    <div class="mapping-card__metrics">
      <span>Revenue <strong>${fmtUSD(row.revenue_usd)}</strong></span>
      <span>Leads <strong>${leadsTxt}</strong></span>
      <span>SQLs <strong>${sqlsTxt}</strong></span>
      <span>Customers <strong>${fmtCount(row.customers)}</strong></span>
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
        <div class="mapping-excluded-note">Not Google Ads — Excluded from Google Ads ROAS.</div>
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
        <p class="revenue-footnote">Map identity only: HubSpot label → Google Ads campaign ID. Spend is sourced only from canonical Google Ads spend — no manual spend entry. Exact normalized matches auto-link; unmatched labels show "Spend mapping unavailable" (never $0). "Mark as Not Google Ads" excludes a label from Google Ads ROAS. Mappings are auditable (approved_by / approved_at) and never overwrite the raw Google Ads campaign identity.</p>
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
    campaignMappingNotice = { type: "ok", msg: `Mapped "${label}" → ${campaignName}. Refresh ROAS by Campaign to see it applied.` };
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
    campaignMappingNotice = { type: "ok", msg: `"${label}" excluded from Google Ads ROAS.` };
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

function junkRateBadge(junkPct) {
  if (junkPct === null || junkPct === undefined) {
    return `<span class="junk-rate-badge junk-rate-badge--none">—</span>`;
  }
  if (junkPct < uiThresholds.junk_rate.low_pct) {
    return `<span class="junk-rate-badge junk-rate-badge--low">${junkPct.toFixed(1)}%</span>`;
  }
  if (junkPct <= uiThresholds.junk_rate.high_pct) {
    return `<span class="junk-rate-badge junk-rate-badge--medium">${junkPct.toFixed(1)}%</span>`;
  }
  return `<span class="junk-rate-badge junk-rate-badge--high">${junkPct.toFixed(1)}%</span>`;
}

// Normalize a country string to a consistent key. Trims whitespace; maps
// null, empty, or whitespace-only values to "(unknown)". Used by both the
// geo performance aggregator and the lead-quality map so merging is lossless.
function normalizeCountryKey(country) {
  const value = (country || "").trim();
  return value || "(unknown)";
}

// ── Geo Intelligence page ──────────────────────────────────────────────────

async function loadGeo() {
  renderPageDatasetFreshness("geo");
  const tableEl = document.getElementById("geo-table-body");
  const mapEl   = document.getElementById("geo-map");
  const mapFallbackEl = document.getElementById("geo-map-fallback");

  if (tableEl) tableEl.innerHTML = `<p class="empty-state" style="padding:var(--space-5)">Loading geo intelligence…</p>`;
  if (mapEl)   mapEl.innerHTML   = "";

  const evWindow = evidenceWindowQuery();

  // Fetch both endpoints; partial failures are tolerated.
  let perfData  = null;
  let leadsData = null;

  try {
    perfData = await fetchJSON(`/api/geo?${evWindow}`);
  } catch (_) { /* geo performance unavailable */ }

  try {
    leadsData = await fetchJSON(`/api/leads/country-summary?${evWindow}`);
  } catch (_) { /* lead country summary unavailable */ }

  // Both failed
  if (!perfData && !leadsData) {
    ["geo-kpi-countries", "geo-kpi-spend", "geo-kpi-sqls", "geo-kpi-junk"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.textContent = "—";
    });
    if (tableEl) tableEl.innerHTML = `
      <div class="geo-empty-state">
        <p class="empty-state">Could not load geo intelligence. Check API health or run status.</p>
      </div>`;
    return;
  }

  // Aggregate geo performance rows by country
  const perfByCountry = new Map();
  for (const row of ((perfData && !perfData.db_unavailable) ? perfData.rows || [] : [])) {
    const key = normalizeCountryKey(row.country);
    if (!perfByCountry.has(key)) {
      perfByCountry.set(key, {
        spend_usd: 0, clicks: 0, impressions: 0, conversions: 0,
        campaigns: new Map(), last_run_date: null,
      });
    }
    const agg = perfByCountry.get(key);
    agg.spend_usd    += row.spend_usd    || 0;
    agg.clicks       += row.clicks       || 0;
    agg.impressions  += row.impressions  || 0;
    agg.conversions  += (row.conversions || 0);
    if (row.campaign_name) {
      const prev = agg.campaigns.get(row.campaign_name) || 0;
      agg.campaigns.set(row.campaign_name, prev + (row.spend_usd || 0));
    }
    if (row.last_run_date && (!agg.last_run_date || row.last_run_date > agg.last_run_date)) {
      agg.last_run_date = row.last_run_date;
    }
  }

  // Build lead quality lookup by country
  const leadsByCountry = new Map();
  for (const row of ((leadsData && !leadsData.db_unavailable) ? leadsData.rows || [] : [])) {
    const key = normalizeCountryKey(row.country);
    leadsByCountry.set(key, row);
  }

  // Merge — union of all countries from both sources
  const allCountries = new Set([...perfByCountry.keys(), ...leadsByCountry.keys()]);

  const merged = [];
  for (const country of allCountries) {
    const perf  = perfByCountry.get(country) || null;
    const leads = leadsByCountry.get(country) || null;

    // Derive top campaign from perf spend
    let topCampaign = (leads && leads.top_campaign) || null;
    if (perf && perf.campaigns.size > 0) {
      topCampaign = [...perf.campaigns.entries()].sort((a, b) => b[1] - a[1])[0][0];
    }

    merged.push({
      country,
      spend_usd:        perf  ? Math.round(perf.spend_usd * 100) / 100 : 0,
      clicks:           perf  ? perf.clicks       : 0,
      impressions:      perf  ? perf.impressions  : 0,
      conversions:      perf  ? Math.round(perf.conversions * 100) / 100 : 0,
      campaigns_count:  perf  ? perf.campaigns.size : 0,
      top_campaign:     topCampaign,
      top_keyword:      leads ? leads.top_keyword   : null,
      total_leads:      leads ? leads.total_leads   : 0,
      confirmed_sqls:   leads ? leads.confirmed_sqls  : 0,
      in_progress:      leads ? leads.in_progress    : 0,
      confirmed_junk:   leads ? leads.confirmed_junk  : 0,
      wrong_fit:        leads ? leads.wrong_fit       : 0,
      unknown:          leads ? leads.unknown         : 0,
      verdicted_leads:  leads ? leads.verdicted_leads : 0,
      junk_rate_pct:    leads ? leads.junk_rate_pct   : null,
      last_run_date:    (perf && perf.last_run_date)
                          ? perf.last_run_date
                          : (leads ? leads.last_run_date : null),
    });
  }

  // Sort: spend desc, then total_leads desc
  merged.sort((a, b) => {
    if (b.spend_usd !== a.spend_usd) return b.spend_usd - a.spend_usd;
    return b.total_leads - a.total_leads;
  });

  geoMergedRows = merged;

  if (merged.length === 0) {
    ["geo-kpi-countries", "geo-kpi-spend", "geo-kpi-sqls", "geo-kpi-junk"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.textContent = "—";
    });
    if (tableEl) tableEl.innerHTML = `
      <div class="geo-empty-state">
        <p class="empty-state">No geo intelligence available for the selected window.</p>
        <p class="geo-empty-subtext">No Google Ads API geo data has been synced yet. Run the weekly or monthly scheduler or check System Status.</p>
      </div>`;
    return;
  }

  // KPI cards
  const countriesActive = merged.filter((r) => r.spend_usd > 0 || r.total_leads > 0).length;
  const totalSpend      = merged.reduce((s, r) => s + r.spend_usd, 0);
  const countriesSQLs   = merged.filter((r) => r.confirmed_sqls > 0).length;
  const highJunk        = merged.filter((r) => r.junk_rate_pct !== null && r.junk_rate_pct >= uiThresholds.junk_rate.high_pct).length;

  const kpiCountriesEl = document.getElementById("geo-kpi-countries");
  const kpiSpendEl     = document.getElementById("geo-kpi-spend");
  const kpiSQLsEl      = document.getElementById("geo-kpi-sqls");
  const kpiJunkEl      = document.getElementById("geo-kpi-junk");

  if (kpiCountriesEl) kpiCountriesEl.textContent = String(countriesActive);
  if (kpiSpendEl)     kpiSpendEl.textContent     = fmtDollar(totalSpend);
  if (kpiSQLsEl)      kpiSQLsEl.textContent      = String(countriesSQLs);
  if (kpiJunkEl)      kpiJunkEl.textContent      = String(highJunk);

  // Map
  renderGeoMap(merged, "total_leads");

  // Wire up metric selector
  const metricSel = document.getElementById("geo-map-metric");
  if (metricSel) {
    metricSel.onchange = () => renderGeoMap(geoMergedRows, metricSel.value);
  }

  // Table
  renderGeoTable(merged);
  // Reapply any active search filter so table stays consistent after reload
  applyGeoSearch();
}

function renderGeoMap(rows, metric) {
  const mapEl         = document.getElementById("geo-map");
  const fallbackEl    = document.getElementById("geo-map-fallback");
  if (!mapEl) return;

  if (!window.Plotly) {
    mapEl.hidden     = true;
    if (fallbackEl) fallbackEl.hidden = false;
    return;
  }
  if (fallbackEl) fallbackEl.hidden = true;
  mapEl.hidden = false;

  const metricLabels = {
    total_leads:    "Leads",
    confirmed_sqls: "SQLs",
    confirmed_junk: "Junk",
    junk_rate_pct:  "Junk Rate %",
    spend_usd:      "Spend (USD)",
  };

  const countries  = rows.map((r) => r.country);
  // junk_rate_pct is intentionally nullable (null = no verdicted leads, not 0% junk).
  // Preserve null for that metric so any future map renderer treats it as missing
  // data rather than zero. Other numeric metrics default to 0 when absent.
  const nullableMetrics = new Set(["junk_rate_pct"]);
  const values = rows.map((r) => {
    const value = r[metric];
    if (value != null) return value;
    return nullableMetrics.has(metric) ? null : 0;
  });

  const customdata = rows.map((r) => [
    r.country,
    r.spend_usd != null ? fmtDollar(r.spend_usd) : "—",
    r.total_leads  || 0,
    r.confirmed_sqls || 0,
    r.confirmed_junk || 0,
    r.junk_rate_pct != null ? r.junk_rate_pct.toFixed(1) + "%" : "—",
    r.top_campaign || "—",
    r.top_keyword  || "—",
  ]);

  const data = [{
    type: "choropleth",
    locationmode: "country names",
    locations:  countries,
    z:          values,
    text:       countries,
    customdata,
    hovertemplate:
      "<b>%{customdata[0]}</b><br>" +
      "Spend: %{customdata[1]}<br>" +
      "Leads: %{customdata[2]}<br>" +
      "SQLs: %{customdata[3]}<br>" +
      "Junk: %{customdata[4]}<br>" +
      "Junk Rate: %{customdata[5]}<br>" +
      "Top Campaign: %{customdata[6]}<br>" +
      "Top Keyword: %{customdata[7]}<extra></extra>",
    colorscale: "Blues",
    showscale: true,
    colorbar: { title: { text: metricLabels[metric] || metric, side: "right" } },
  }];

  const layout = {
    margin: { l: 0, r: 0, t: 0, b: 0 },
    geo: {
      showframe: false,
      showcoastlines: true,
      projection: { type: "natural earth" },
    },
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor:  "rgba(0,0,0,0)",
  };

  window.Plotly.react(mapEl, data, layout, { responsive: true, displayModeBar: false });
}

function renderGeoTable(rows) {
  const tableEl = document.getElementById("geo-table-body");
  if (!tableEl) return;

  if (rows.length === 0) {
    tableEl.innerHTML = `<p class="empty-state" style="padding:var(--space-5)">No countries to display.</p>`;
    return;
  }

  const thead = `
    <thead>
      <tr>
        <th>Country</th>
        <th class="td--num">Spend</th>
        <th class="td--num">Clicks</th>
        <th class="td--num">Conv.</th>
        <th class="td--num">Leads</th>
        <th class="td--num">SQLs</th>
        <th class="td--num">In Progress</th>
        <th class="td--num">Junk</th>
        <th>Junk Rate</th>
        <th>Top Campaign</th>
        <th>Top Keyword</th>
        <th>Last Run</th>
      </tr>
    </thead>`;

  const tbody = rows.map((r) => {
    const badge = junkRateBadge(r.junk_rate_pct);

    return `
      <tr data-country="${escapeHtml(r.country)}"
          data-campaign="${escapeHtml(r.top_campaign || "")}"
          data-keyword="${escapeHtml(r.top_keyword || "")}">
        <td class="td--name">${escapeHtml(r.country)}</td>
        <td class="td--num">${r.spend_usd > 0 ? fmtDollar(r.spend_usd) : "—"}</td>
        <td class="td--num">${r.clicks > 0 ? r.clicks : "—"}</td>
        <td class="td--num">${r.conversions > 0 ? r.conversions.toFixed(1) : "—"}</td>
        <td class="td--num">${r.total_leads > 0 ? r.total_leads : "—"}</td>
        <td class="td--num">${r.confirmed_sqls > 0 ? r.confirmed_sqls : "—"}</td>
        <td class="td--num">${r.in_progress > 0 ? r.in_progress : "—"}</td>
        <td class="td--num">${r.confirmed_junk > 0 ? r.confirmed_junk : "—"}</td>
        <td>${badge}</td>
        <td>${escapeHtml(r.top_campaign || "—")}</td>
        <td>${escapeHtml(r.top_keyword || "—")}</td>
        <td>${escapeHtml(r.last_run_date || "—")}</td>
      </tr>`;
  }).join("");

  tableEl.innerHTML = `<table class="data-table">${thead}<tbody>${tbody}</tbody></table>`;
}

function applyGeoSearch() {
  const search = (document.getElementById("geo-search") || {}).value || "";
  const term   = search.trim().toLowerCase();
  const tableEl = document.getElementById("geo-table-body");
  if (!tableEl) return;

  const rows = tableEl.querySelectorAll("tr[data-country]");
  rows.forEach((row) => {
    const country  = (row.dataset.country  || "").toLowerCase();
    const campaign = (row.dataset.campaign || "").toLowerCase();
    const keyword  = (row.dataset.keyword  || "").toLowerCase();
    const match    = !term || country.includes(term) || campaign.includes(term) || keyword.includes(term);
    row.hidden = !match;
  });
}

// ── Keywords page ──────────────────────────────────────────────────────────

function normalizeMatchType(value) {
  const text = (value || "").toLowerCase().trim();
  if (text.includes("broad")) return "broad";
  if (text.includes("phrase")) return "phrase";
  if (text.includes("exact")) return "exact";
  return "unknown";
}

function matchTypeBadge(value) {
  const norm = normalizeMatchType(value);
  const labels = { broad: "Broad", phrase: "Phrase", exact: "Exact", unknown: "Unknown" };
  return `<span class="match-type-badge match-type-${norm}">${labels[norm]}</span>`;
}

function qualityScoreBadge(qs) {
  if (qs === null || qs === undefined) {
    return `<span class="quality-score-badge quality-score-none">—</span>`;
  }
  const n = parseFloat(qs);
  if (isNaN(n)) return `<span class="quality-score-badge quality-score-none">—</span>`;
  let cls;
  if (n >= uiThresholds.quality_score.strong_min)      cls = "quality-score-strong";
  else if (n >= uiThresholds.quality_score.medium_min) cls = "quality-score-medium";
  else                                                  cls = "quality-score-weak";
  return `<span class="quality-score-badge ${cls}">${n.toFixed(1)}</span>`;
}

// ── Search Terms + Patterns evidence page (PR-ADS-144) ──────────────────────
// One full-width decision-support page with two tabs (Terms | Patterns),
// aligned with the Campaign Evidence design. Data comes from the
// /api/search-term-evidence family: durable source_date-bounded selected-window
// aggregates, tri-state review states, reported-search-term-spend semantics,
// complete-population KPIs and server-side pagination. Strictly read-only —
// nothing here adds negatives or writes to Google Ads / HubSpot.

let _stTab = "terms";               // terms | patterns
let _stData = null;                  // /api/search-term-evidence payload
let _stLoadState = "idle";           // idle | loading | ok | db_unavailable | error
let _stFilters = { q: "", campaign: "", state: "", junk: "", minSpend: "", sort: "spend", page: 1 };
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

// Compact tri-state mix pills (still used by the Campaign Evidence drawer's
// N-Gram drilldown section — Campaign Evidence layout is unchanged).
function ngramWasteStateMix(flagged, clean, unanalyzed) {
  const parts = [];
  if (flagged)    parts.push(`<span class="ngram-state-flagged" title="Flagged waste rows">${flagged}F</span>`);
  if (clean)      parts.push(`<span class="ngram-state-clean" title="Reviewed clean rows">${clean}C</span>`);
  if (unanalyzed) parts.push(`<span class="ngram-state-unanalyzed" title="Needs-review rows">${unanalyzed}U</span>`);
  return parts.length ? `<span class="ngram-state-mix">${parts.join(" ")}</span>` : "—";
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
  // Currency-aware spend display: prefers USD, falls back to native with disclosure.
  if (row.spend_usd !== null && row.spend_usd !== undefined) return stMoney(row.spend_usd);
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
  _stFilters.page = 1; // fresh page entry / window change → restart pagination
  // Deep link: #/search-terms?tab=patterns (also produced by the #/ngrams alias).
  const rawHash = window.location.hash || "";
  const hashQuery = (rawHash.split("?")[1] || "");
  if (rawHash.startsWith("#/ngrams") ||
      new URLSearchParams(hashQuery).get("tab") === "patterns") {
    _stTab = "patterns";
  }
  // Campaign-drawer prefill (navigate("ngrams") with a campaign context).
  if (ngramsPrefill && ngramsPrefill.campaign) {
    _patPendingCampaignName = ngramsPrefill.campaign;
    _stTab = "patterns";
    ngramsPrefill = null;
  }
  renderSearchTermsShell();
  if (_stTab === "patterns") loadPatternsTab();
  else loadTermsTab();
}

// Back-compat entry point (older callers/tests referenced loadSearchTerms).
function loadSearchTerms() { loadSearchTermsEvidence(); }

function renderSearchTermsShell() {
  const shell = document.getElementById("search-terms-shell");
  if (!shell) return;
  shell.innerHTML = `
    <header class="campaign-evidence-head">
      <div class="campaign-evidence-head__text">
        <h2 class="campaign-evidence-title">Search Terms</h2>
        <p class="campaign-evidence-sub">Queries reported by Google Ads, with review state and waste evidence.</p>
      </div>
      <span class="campaign-evidence-window" title="Selected evidence window">${escapeHtml(stWindowLabel())}</span>
    </header>
    <div class="search-terms-tabs" role="tablist" aria-label="Search Terms tabs">
      <button id="tab-btn-terms" class="search-terms-tab${_stTab === "terms" ? " active" : ""}"
        role="tab" aria-selected="${_stTab === "terms"}" data-tab="terms" type="button">Terms</button>
      <button id="tab-btn-patterns" class="search-terms-tab${_stTab === "patterns" ? " active" : ""}"
        role="tab" aria-selected="${_stTab === "patterns"}" data-tab="patterns" type="button">Patterns</button>
    </div>
    <div id="st-tab-body"></div>`;
  shell.querySelectorAll(".search-terms-tab").forEach((tab) => {
    tab.addEventListener("click", () => stActivateTab(tab.dataset.tab));
  });
}

function stActivateTab(tab) {
  if (tab !== "terms" && tab !== "patterns") return;
  _stTab = tab;
  document.querySelectorAll(".search-terms-tab").forEach((t) => {
    const active = t.dataset.tab === tab;
    t.classList.toggle("active", active);
    t.setAttribute("aria-selected", String(active));
  });
  // Keep the deep link honest without triggering the router.
  const newHash = tab === "patterns" ? "#/search-terms?tab=patterns" : "#/search-terms";
  if (window.location.hash !== newHash) {
    history.replaceState(history.state, "", newHash);
  }
  if (tab === "patterns") loadPatternsTab();
  else loadTermsTab();
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
    <div class="evidence-table-shell">
      <div class="evidence-table-scroll" id="st-terms-table"></div>
      <div class="st-pagination" id="st-terms-pagination"></div>
    </div>`;
  renderTermsTable();
  wireTermsControls();
}

function renderTermsKPIs(kpis) {
  if (!kpis) return "";
  const coverage = kpis.coverage || {};
  const val = (v) => (v === null || v === undefined) ? stUnavailable() : fmtCount(v);
  const cards = [
    stKpiCard("Reported Terms", val(kpis.reported_terms), "Term × campaign rows in window"),
    stKpiCard("Reported Search-Term Spend",
      kpis.reported_spend_usd === null || kpis.reported_spend_usd === undefined
        ? stUnavailable() : stMoney(kpis.reported_spend_usd),
      kpis.currency_status === "proven" && kpis.fx_complete
        ? "FX-converted to USD — reported by Google Ads"
        : kpis.reported_spend_usd === null || kpis.reported_spend_usd === undefined
          ? "USD unavailable — FX incomplete or currency unproven"
          : "Reported by Google Ads — not total account spend"),
    stKpiCard("Clicks", val(kpis.clicks), ""),
    stKpiCard("Flagged Waste", val(kpis.flagged_waste), "Human-review candidates"),
    stKpiCard("Needs Review", val(kpis.needs_review), "Not yet classified"),
  ];
  // Coverage is a WINDOW-LEVEL source-completeness diagnostic — it does not
  // narrow with the row filters, so showing it next to filtered KPIs would be
  // internally inconsistent. Hide the card whenever filters are active.
  if (coverage.status === "ok" && coverage.coverage_pct !== null &&
      coverage.coverage_pct !== undefined && !stHasActiveFilters()) {
    cards.push(stKpiCard("Reporting Coverage",
      `${Number(coverage.coverage_pct).toFixed(1)}%`,
      `Search-term reporting coverage of ${stMoney(coverage.canonical_spend_usd)} canonical spend (whole window)`));
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
    <input type="number" id="st-min-spend" min="0" step="1" class="waste-search-input st-min-spend"
      placeholder="Min spend $" value="${escapeHtml(String(_stFilters.minSpend))}"
      aria-label="Minimum reported spend in USD" />
    <select id="st-sort" class="waste-filter-select" aria-label="Sort">
      <option value="spend"${_stFilters.sort === "spend" ? " selected" : ""}>Highest spend</option>
      <option value="clicks"${_stFilters.sort === "clicks" ? " selected" : ""}>Most clicks</option>
      <option value="cpc"${_stFilters.sort === "cpc" ? " selected" : ""}>Highest CPC</option>
      <option value="conversions"${_stFilters.sort === "conversions" ? " selected" : ""}>Most platform conversions</option>
      <option value="last_seen"${_stFilters.sort === "last_seen" ? " selected" : ""}>Newest seen</option>
      <option value="term"${_stFilters.sort === "term" ? " selected" : ""}>Search term</option>
    </select>
    <button id="st-reset" type="button" class="revenue-filter-chip campaign-clear-btn">Reset</button>
    <button id="st-export" type="button" class="revenue-filter-chip campaign-clear-btn"
      title="Export the complete server-filtered dataset for this window">Export CSV</button>
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
      <td class="td--num" data-label="Last Seen">${r.last_seen ? escapeHtml(r.last_seen) : "—"}</td>
      <td class="td--action" data-label=""><span class="campaign-open-icon" aria-hidden="true" title="Open evidence">›</span></td>
    </tr>`).join("");

  host.innerHTML = `<table class="data-table campaign-decision-table st-table">
    <thead><tr>
      <th class="td--name">Search Term</th><th>State</th><th>Campaign</th>
      <th class="td--num">Spend</th><th class="td--num">Clicks</th>
      <th class="td--num">CPC</th><th class="td--num">Last Seen</th>
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
  [["st-campaign", "campaign"], ["st-state", "state"], ["st-junk", "junk"], ["st-sort", "sort"]]
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
    _stFilters = { q: "", campaign: "", state: "", junk: "", minSpend: "", sort: "spend", page: 1 };
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
      kpis.reported_spend_represented_usd === null || kpis.reported_spend_represented_usd === undefined
        ? stUnavailable() : stMoney(kpis.reported_spend_represented_usd),
      "Unique underlying terms — not additive across pattern rows"),
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

  const dailyRows = (daily.rows || []).map((d) => `
    <tr><td>${escapeHtml(d.source_date || "")}</td>
      <td class="td--num">${stMoney(d.spend_usd)}</td>
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
        ${stDrawerKpi("Reported Spend", stMoney(t.spend_usd))}
        ${stDrawerKpi("Clicks", fmtCount(t.clicks))}
        ${stDrawerKpi("Impressions", fmtCount(t.impressions))}
        ${stDrawerKpi("CPC", stMoney(t.cpc_usd))}
        ${stDrawerKpi("First Seen", t.first_seen ? escapeHtml(t.first_seen) : "—")}
        ${stDrawerKpi("Last Seen", t.last_seen ? escapeHtml(t.last_seen) : "—")}
      </div>
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
        ${factRow("Junk category", cls.junk_categories && cls.junk_categories.length ? escapeHtml(cls.junk_categories.join(", ")) : "—")}
        ${factRow("Matched rule / pattern", cls.matched_patterns && cls.matched_patterns.length ? escapeHtml(cls.matched_patterns.join(", ")) : "—")}
        ${factRow("CRM-junk confirmations", cls.crm_junk_confirmed !== null && cls.crm_junk_confirmed !== undefined ? fmtCount(cls.crm_junk_confirmed) : stUnavailable())}
        ${factRow("Classified on", cls.classification_date ? escapeHtml(cls.classification_date) : stUnavailable())}
        ${factRow("Classification source", cls.classification_source ? escapeHtml(cls.classification_source) : stUnavailable())}
      </tbody></table>
      <p class="drawer-source-note">Flagged waste = stored is_flagged_waste true (a human-review candidate, not an approved negative). Reviewed clean = stored false. Needs review = not yet classified.</p>
    </div>
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
        ? `<table class="drawer-table"><thead><tr><th>Date</th><th class="td--num">Spend</th><th class="td--num">Clicks</th><th class="td--num">Impr.</th></tr></thead><tbody>${dailyRows}</tbody></table>`
        : `<p class="drawer-empty">No per-day rows reported in this window.</p>`}
      <p class="drawer-source-note">Only dates the source actually reported are shown — missing dates are never fabricated as zero.</p>
    </div>
    <div class="drawer-section">
      <div class="drawer-section__title">Links</div>
      <div class="st-drawer-links">
        ${mapped ? `<button type="button" class="revenue-filter-chip" id="st-link-campaign">Open Campaign Evidence</button>` : ""}
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
      navigate("campaigns", { updateHash: true });
      setTimeout(() => {
        if (typeof openCampaignDrawer === "function") {
          openCampaignDrawer(c.campaign_name, c.campaign_key);
        }
      }, 250);
    });
  }
  const linkWaste = document.getElementById("st-link-waste");
  if (linkWaste) linkWaste.addEventListener("click", () => {
    closeStDrawer();
    navigate("waste", { updateHash: true });
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

function downloadWasteCSV() {
  const items = getFilteredWasteTerms();
  if (!items.length) return;
  const headers = [
    "Search Term", "Campaign", "Spend USD", "Junk Category",
    "Matched Pattern", "CRM Junk Confirmed", "Run Date",
  ];
  const rows = items.map((t) => [
    t.search_term, t.campaign_name, t.spend_usd,
    t.junk_category || "", t.matched_pattern || "",
    t.crm_junk_confirmed, t.run_date,
  ]);
  // PR-ADS-141: when the row list is a partial page, name the file so it never
  // implies the complete waste library for the window.
  const suffix = _wasteMeta.truncated ? `_partial_first${_wasteMeta.returned_count}` : "";
  downloadCSV(`waste_terms_${getEvidenceWindow()}${suffix}.csv`, headers, rows);
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

function _gclidMiniStatusBadge(matchStatus) {
  const normalized = (matchStatus || "unknown").toLowerCase();
  const map = {
    matched: { label: "Matched", cls: "gclid-mini-status-matched" },
    url_fallback: { label: "URL Fallback", cls: "gclid-mini-status-url-fallback" },
    unmatched: { label: "Unmatched", cls: "gclid-mini-status-unmatched" },
    unknown: { label: "Unknown", cls: "gclid-mini-status-unknown" },
  };
  const value = map[normalized] || map.unknown;
  return `<span class="gclid-mini-status-badge ${value.cls}">${value.label}</span>`;
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

async function loadKeywords() {
  renderPageDatasetFreshness("keywords");
  const tableEl   = document.getElementById("kw-table-body");
  const summaryEl = document.getElementById("kw-matchtype-summary");
  if (summaryEl) summaryEl.innerHTML = "";

  ["kw-kpi-spend", "kw-kpi-active", "kw-kpi-broad", "kw-kpi-qs"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.textContent = "—";
  });

  keywordLoadStatus = "loading";

  try {
    const data = await fetchJSON(`/api/keywords?${evidenceWindowQuery()}`);

    if (data.db_unavailable) {
      keywordLoadStatus = "db_unavailable";
      keywordRows = [];
      renderKeywordsTable([]);
      return;
    }

    keywordRows = data.rows || [];

    if (keywordRows.length === 0) {
      keywordLoadStatus = "empty";
      renderKeywordsTable([]);
      return;
    }

    keywordLoadStatus = "ok";
    renderKeywordsKPIs(keywordRows);
    renderMatchTypeSummary(keywordRows);
    populateKeywordFilters(keywordRows);
    // PR-ADS-142: apply a campaign prefill from the campaign drawer, if present.
    if (keywordsPrefill && keywordsPrefill.campaign) {
      const sel = document.getElementById("kw-filter-campaign");
      if (sel && Array.from(sel.options).some((o) => o.value === keywordsPrefill.campaign)) {
        sel.value = keywordsPrefill.campaign;
      }
      keywordsPrefill = null;
    }
    applyKeywordFilters();

  } catch (_) {
    keywordLoadStatus = "error";
    keywordRows = [];
    renderKeywordsTable([]);
  }
}

function keywordDedupeKey(r) {
  return `${(r.keyword || "").toLowerCase()}|${normalizeMatchType(r.match_type)}|${(r.campaign_name || "").toLowerCase()}`;
}

function renderKeywordsKPIs(rows) {
  const totalSpend = rows.reduce((s, r) => s + (r.spend_usd || 0), 0);

  const activeKeywords = new Set(rows.map(keywordDedupeKey)).size;

  const broadSpend = rows
    .filter((r) => normalizeMatchType(r.match_type) === "broad")
    .reduce((s, r) => s + (r.spend_usd || 0), 0);

  const qsRows = rows.filter((r) => r.quality_score != null);
  const avgQS  = qsRows.length > 0
    ? qsRows.reduce((s, r) => s + r.quality_score, 0) / qsRows.length
    : null;

  const spendEl  = document.getElementById("kw-kpi-spend");
  const activeEl = document.getElementById("kw-kpi-active");
  const broadEl  = document.getElementById("kw-kpi-broad");
  const qsEl     = document.getElementById("kw-kpi-qs");

  if (spendEl)  spendEl.textContent  = fmtDollar(totalSpend);
  if (activeEl) activeEl.textContent = String(activeKeywords);
  if (broadEl)  broadEl.textContent  = fmtDollar(broadSpend);
  if (qsEl)     qsEl.textContent     = avgQS != null ? avgQS.toFixed(1) : "—";
}

function renderMatchTypeSummary(rows) {
  const el = document.getElementById("kw-matchtype-summary");
  if (!el) return;

  const types  = ["broad", "phrase", "exact", "unknown"];
  const labels = { broad: "Broad", phrase: "Phrase", exact: "Exact", unknown: "Unknown" };

  const grouped = {};
  types.forEach((t) => { grouped[t] = { keys: new Set(), spend: 0, clicks: 0, conversions: 0 }; });

  for (const row of rows) {
    const t = normalizeMatchType(row.match_type);
    // Count distinct keyword texts per match type (same keyword in multiple ad groups counts once)
    grouped[t].keys.add((row.keyword || "").trim().toLowerCase());
    grouped[t].spend       += row.spend_usd    || 0;
    grouped[t].clicks      += row.clicks       || 0;
    grouped[t].conversions += row.conversions  || 0;
  }

  const cards = types.map((t) => {
    const g = grouped[t];
    if (g.keys.size === 0) return "";
    return `
      <div class="matchtype-card">
        <div class="matchtype-card__header">
          <span class="match-type-badge match-type-${t}">${labels[t]}</span>
        </div>
        <div class="matchtype-card__stats">
          <div class="matchtype-card__stat">
            <span class="matchtype-card__num">${g.keys.size}</span>
            <span class="matchtype-card__label">keywords</span>
          </div>
          <div class="matchtype-card__stat">
            <span class="matchtype-card__num">${fmtDollar(g.spend)}</span>
            <span class="matchtype-card__label">spend</span>
          </div>
          <div class="matchtype-card__stat">
            <span class="matchtype-card__num">${g.clicks}</span>
            <span class="matchtype-card__label">clicks</span>
          </div>
          <div class="matchtype-card__stat">
            <!-- Google Ads conversions are fractional due to cross-device attribution -->
            <span class="matchtype-card__num">${g.conversions.toFixed(1)}</span>
            <span class="matchtype-card__label">Google conv.</span>
          </div>
        </div>
      </div>`;
  }).join("");

  el.innerHTML = cards || `<p class="empty-state">No match type data available.</p>`;
}

function populateKeywordFilters(rows) {
  const campSel = document.getElementById("kw-filter-campaign");
  if (campSel) {
    const camps = [...new Set(rows.map((r) => r.campaign_name).filter(Boolean))].sort();
    campSel.innerHTML = `<option value="">All campaigns</option>` +
      camps.map((c) => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("");
  }
}

function getFilteredKeywordRows() {
  const searchInput = document.getElementById("kw-filter-search");
  const campSel     = document.getElementById("kw-filter-campaign");
  const matchSel    = document.getElementById("kw-filter-matchtype");

  const search = searchInput ? searchInput.value.trim().toLowerCase() : "";
  const camp   = campSel     ? campSel.value  : "";
  const match  = matchSel    ? matchSel.value : "";

  let filtered = keywordRows;
  if (search) {
    filtered = filtered.filter((r) =>
      (r.keyword       || "").toLowerCase().includes(search) ||
      (r.campaign_name || "").toLowerCase().includes(search) ||
      (r.ad_group      || "").toLowerCase().includes(search)
    );
  }
  if (camp)  filtered = filtered.filter((r) => r.campaign_name === camp);
  if (match) filtered = filtered.filter((r) => normalizeMatchType(r.match_type) === match);
  return filtered;
}

function applyKeywordFilters() {
  if (keywordLoadStatus !== "ok") {
    renderKeywordsTable([]);
    return;
  }
  renderKeywordsTable(getFilteredKeywordRows());
}

// Keyword rows with spend at or above uiThresholds.spend.high_spend_usd get subtle high-spend emphasis.
// Value is loaded from /api/config/ui-thresholds after auth; falls back to DEFAULT_UI_THRESHOLDS.

function renderKeywordsTable(rows) {
  const tableEl = document.getElementById("kw-table-body");
  if (!tableEl) return;

  if (keywordLoadStatus === "db_unavailable") {
    tableEl.innerHTML = `
      <div class="keywords-empty-state">
        <p class="empty-state">Keyword data temporarily unavailable — database offline.</p>
      </div>`;
    return;
  }

  if (keywordLoadStatus === "error") {
    tableEl.innerHTML = `
      <div class="keywords-empty-state">
        <p class="empty-state">Could not load keyword data. Check API health or run status.</p>
      </div>`;
    return;
  }

  if (rows.length === 0) {
    if (keywordLoadStatus === "empty") {
      tableEl.innerHTML = `
        <div class="keywords-empty-state">
          <p class="empty-state">No keyword data available for the selected window.</p>
          <p class="keywords-empty-subtext">No Google Ads API keyword data has been synced yet. Run the weekly or monthly scheduler or check System Status.</p>
        </div>`;
    } else {
      tableEl.innerHTML =
        `<p class="empty-state" style="padding:var(--space-5)">No results match the current filter.</p>`;
    }
    return;
  }

  const sorted = [...rows].sort((a, b) => (b.spend_usd || 0) - (a.spend_usd || 0));

  const thead = `
    <thead>
      <tr>
        <th>Keyword</th>
        <th>Match Type</th>
        <th>Campaign</th>
        <th>Ad Group</th>
        <th class="td--num">Spend</th>
        <th class="td--num">Clicks</th>
        <th class="td--num">Impressions</th>
        <th class="td--num">CPC</th>
        <th class="td--num">Google Conv.</th>
        <th>Quality Score</th>
        <th class="td--num">Runs</th>
        <th>Last Run</th>
      </tr>
    </thead>`;

  const tbody = sorted.map((r) => {
    const highSpend = (r.spend_usd || 0) >= uiThresholds.spend.high_spend_usd;
    return `
      <tr${highSpend ? ' class="row--high-spend"' : ""}>
        <td class="td--name">${escapeHtml(r.keyword || "—")}</td>
        <td>${matchTypeBadge(r.match_type)}</td>
        <td>${escapeHtml(r.campaign_name || "—")}</td>
        <td>${escapeHtml(r.ad_group || "—")}</td>
        <td class="td--num${highSpend ? " waste-spend--high" : ""}">${r.spend_usd != null ? fmtDollar(r.spend_usd) : "—"}</td>
        <td class="td--num">${r.clicks != null ? r.clicks : "—"}</td>
        <td class="td--num">${r.impressions != null ? r.impressions : "—"}</td>
        <td class="td--num">${r.cpc_usd != null ? "$" + r.cpc_usd.toFixed(2) : "—"}</td>
        <td class="td--num">${r.conversions != null ? r.conversions.toFixed(1) : "—"}</td>
        <td>${qualityScoreBadge(r.quality_score)}</td>
        <td class="td--num">${r.runs != null ? r.runs : "—"}</td>
        <td>${r.last_run_date ? escapeHtml(r.last_run_date) : "—"}</td>
      </tr>`;
  }).join("");

  tableEl.innerHTML = `<table class="data-table">${thead}<tbody>${tbody}</tbody></table>`;
}

function copyKeywordRows() {
  const rows = getFilteredKeywordRows();
  if (rows.length === 0) return;

  const sorted = [...rows].sort((a, b) => (b.spend_usd || 0) - (a.spend_usd || 0));
  const headers = ["keyword", "match_type", "campaign_name", "spend_usd", "clicks", "conversions"];
  const lines = [headers.join("\t")].concat(
    sorted.map((r) => [
      r.keyword        || "",
      normalizeMatchType(r.match_type),
      r.campaign_name  || "",
      r.spend_usd      != null ? r.spend_usd.toFixed(2)      : "",
      r.clicks         != null ? String(r.clicks)             : "",
      r.conversions    != null ? r.conversions.toFixed(1)     : "",
    ].join("\t"))
  );
  const text = lines.join("\n");

  const btn      = document.getElementById("kw-copy-btn");
  const origHTML = btn ? btn.innerHTML : null;

  const showFeedback = (success) => {
    if (!btn) return;
    btn.innerHTML = success
      ? `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg> Copied!`
      : `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg> Copy failed`;
    btn.disabled = true;
    setTimeout(() => {
      if (btn && origHTML !== null) { btn.innerHTML = origHTML; btn.disabled = false; }
    }, 2000);
  };

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(
      () => showFeedback(true),
      () => showFeedback(false),
    );
  } else {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity  = "0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) { /* ignore */ }
    document.body.removeChild(ta);
    showFeedback(ok);
  }
}

// ── In Progress Leads page ─────────────────────────────────────────────────

// Explicit MDR workflow statuses shown on the In Progress Leads page.
// OPEN - Connecting has status_category=unknown in the data model (backend
// classification is not changed); it is included here because it is an active
// work-queue status that MDR is still handling, not a final verdict.
const ACTIVE_MDR_STATUSES = new Set([
  "OPEN - Meeting Booked",
  "OPEN - Pending Meeting",
  "OPEN - Connecting",
]);

async function loadOpportunities() {
  renderPageDatasetFreshness("in_progress_leads");
  const el = document.getElementById("opps-body");
  if (!el) return;

  el.innerHTML = `<p class="empty-state">Loading in-progress leads…</p>`;

  try {
    const data  = await fetchJSON(`/api/leads?${evidenceWindowQuery()}`);

    // Deduplicate by contact_id (same null-safe approach as loadLeads)
    const seen = new Map();
    for (const [index, lead] of (data.leads || []).entries()) {
      const dedupeKey = hasValidContactId(lead)
        ? `contact:${lead.contact_id}`
        : `row:${index}`;
      const existing = seen.get(dedupeKey);
      if (!existing || lead.run_date > existing.run_date) {
        seen.set(dedupeKey, lead);
      }
    }

    // Filter by explicit MDR active statuses — includes OPEN - Connecting even
    // though its status_category is "unknown" in the backend classification.
    const inProgress = Array.from(seen.values())
      .filter((l) => ACTIVE_MDR_STATUSES.has(l.mql_status));

    if (inProgress.length === 0) {
      el.innerHTML = `<p class="empty-state">No in-progress leads in the selected window.</p>`;
      return;
    }

    // Group by mql_status
    const booked     = inProgress.filter((l) => l.mql_status === "OPEN - Meeting Booked");
    const pending    = inProgress.filter((l) => l.mql_status === "OPEN - Pending Meeting");
    const connecting = inProgress.filter((l) => l.mql_status === "OPEN - Connecting");

    const renderGroup = (title, leads) => {
      if (leads.length === 0) return "";
      return `
        <p class="opp-group-title">${escapeHtml(title)} (${leads.length})</p>
        <div class="opp-grid">
          ${leads.map((l) => {
            const cardTitle = l.company || l.keyword || l.country || "Unnamed lead";
            const isConnecting = l.mql_status === "OPEN - Connecting";
            return `
            <div class="opp-card">
              <div class="opp-card__company">${escapeHtml(cardTitle)}</div>
              <div class="opp-card__meta">
                <span class="opp-card__tag">${escapeHtml(l.mql_status || "In Progress")}</span>
                ${isConnecting ? `<span class="opp-card__tag opp-card__tag--muted">No verdict yet</span>` : ""}
                ${l.campaign_name ? `<span class="opp-card__tag">${escapeHtml(l.campaign_name)}</span>` : ""}
                ${l.keyword ? `<span class="opp-card__tag">${escapeHtml(l.keyword)}</span>` : ""}
                ${l.country ? `<span class="opp-card__tag">${escapeHtml(l.country)}</span>` : ""}
              </div>
              ${l.contact_id ? `<div style="font-size:11px;color:var(--text-muted);margin-top:4px">ID: ${escapeHtml(l.contact_id)}</div>` : ""}
            </div>`;
          }).join("")}
        </div>`;
    };

    // PR-ADS-141: the leads row list is a presentation-capped page. When the
    // window holds more leads than were returned, disclose it — never imply the
    // in-progress list is complete for All time.
    const truncationNote = data.has_more
      ? `<div class="evidence-status-chip evidence-status-chip--warning" style="margin-bottom:12px;display:inline-flex">`
        + `Showing in-progress leads from the ${fmtCount(data.returned_count)} most recent of `
        + `${fmtCount(data.total_count)} leads in this window — narrow the window for a complete list.</div>`
      : "";

    el.innerHTML = truncationNote
                 + renderGroup("Meeting Booked", booked)
                 + renderGroup("Pending Meeting", pending)
                 + renderGroup("Connecting", connecting);

  } catch (_) {
    el.innerHTML = `<p class="empty-state">Could not load in-progress lead data.</p>`;
  }
}

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
      const rp = data.result && data.result.report_path
        ? ` Report: ${escapeHtml(data.result.report_path)}`
        : "";
      showSchedFeedback("success", `Run completed. Finished at ${escapeHtml(data.finished_at || "—")}.${rp}`);
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

// ── Historical Intelligence page ──────────────────────────────────────────

const _HI_TREND_LABELS = {
  improving:          "Improving",
  deteriorating:      "Deteriorating",
  stable:             "Stable",
  insufficient_data:  "Insufficient Data",
  new_activity:       "New Activity",
  no_recent_activity: "No Recent Activity",
};

const _HI_TREND_CLASS = {
  improving:          "badge--ok",
  deteriorating:      "badge--error",
  stable:             "badge--neutral",
  insufficient_data:  "badge--neutral",
  new_activity:       "badge--neutral",
  no_recent_activity: "badge--neutral",
};

function _hiMovementArrow(dir) {
  if (!dir) return "—";
  if (dir === "up")    return "↑";
  if (dir === "down")  return "↓";
  if (dir === "stable") return "—";
  if (dir === "better") return "improved";
  if (dir === "worse")  return "worsened";
  if (dir === "insufficient_data" || dir === "no_recent_activity" || dir === "new_activity") return "—";
  return escapeHtml(dir);
}

async function loadHistoricalIntelligence() {
  const bodyEl       = document.getElementById("hi-trend-body");
  const kpiImproving = document.getElementById("hi-kpi-improving");
  const kpiDeter     = document.getElementById("hi-kpi-deteriorating");
  const kpiStable    = document.getElementById("hi-kpi-stable");
  const kpiInsuf     = document.getElementById("hi-kpi-insufficient");

  const entitySel  = document.getElementById("hi-entity-select");
  const curSel     = document.getElementById("hi-current-days-select");
  const prevSel    = document.getElementById("hi-previous-days-select");

  const entity      = entitySel  ? entitySel.value  : "campaigns";
  const currentDays = curSel     ? curSel.value      : "30";
  const previousDays = prevSel   ? prevSel.value     : "30";

  if (bodyEl) {
    bodyEl.innerHTML = `<p class="empty-state" style="padding:var(--space-5)"><span class="spinner"></span> Loading historical intelligence…</p>`;
  }
  hiStatus = "loading";

  const url = `/api/historical-intelligence?entity=${encodeURIComponent(entity)}&current_days=${encodeURIComponent(currentDays)}&previous_days=${encodeURIComponent(previousDays)}&limit=50`;

  try {
    const data = await fetchJSON(url);
    hiStatus  = data.status || "ok";
    hiRows    = data.rows   || [];
    hiSummary = data.summary || null;

    // KPI cards
    const sum = hiSummary || {};
    if (kpiImproving) kpiImproving.textContent = sum.improving     ?? "—";
    if (kpiDeter)     kpiDeter.textContent     = sum.deteriorating ?? "—";
    if (kpiStable)    kpiStable.textContent    = sum.stable        ?? "—";
    if (kpiInsuf)     kpiInsuf.textContent     = sum.insufficient_data ?? "—";

    _renderHistoricalTable(entity, data);

  } catch (err) {
    hiStatus = "error";
    hiRows   = [];
    hiSummary = null;
    if (kpiImproving) kpiImproving.textContent = "—";
    if (kpiDeter)     kpiDeter.textContent     = "—";
    if (kpiStable)    kpiStable.textContent    = "—";
    if (kpiInsuf)     kpiInsuf.textContent     = "—";
    if (bodyEl) {
      if (err && err.message === "HTTP 401") {
        showLogin();
        return;
      }
      const msg = err && err.message === "HTTP 403"
        ? "Access denied."
        : "Failed to load historical intelligence. Please try again.";
      bodyEl.innerHTML = `<p class="empty-state run-feedback--error" style="padding:var(--space-5)">${escapeHtml(msg)}</p>`;
    }
  }
}

function _renderHistoricalTable(entity, data) {
  const bodyEl = document.getElementById("hi-trend-body");
  if (!bodyEl) return;

  if (data && data.db_unavailable) {
    bodyEl.innerHTML = `<p class="empty-state" style="padding:var(--space-5)">Historical Intelligence is unavailable because the local database is offline.</p>`;
    return;
  }

  if (hiStatus === "insufficient_data" || !hiRows || hiRows.length === 0) {
    const _fallback = "Insufficient historical data. At least two comparable periods are required before trend signals can be computed.";
    const msg = (data && typeof data.message === "string" && data.message) ? data.message : _fallback;
    bodyEl.innerHTML = `<p class="empty-state" style="padding:var(--space-5)">${escapeHtml(msg)}</p>`;
    return;
  }

  const isGeo = entity === "geo";

  const headerCols = isGeo
    ? ["Country", "Campaign", "Trend", "Spend Movement", "Human Review Note"]
    : ["Campaign", "Trend", "Spend Movement", "SQL Movement", "Junk Rate Movement", "CPQL Movement", "Human Review Note"];

  const headerHtml = headerCols.map((h) => `<th>${escapeHtml(h)}</th>`).join("");

  const rowsHtml = hiRows.map((row) => {
    const trendLabel = _HI_TREND_LABELS[row.trend_status] || escapeHtml(row.trend_status || "—");
    const trendCls   = _HI_TREND_CLASS[row.trend_status]  || "badge--neutral";
    const trendBadge = `<span class="badge ${trendCls}"><span class="dot"></span>${escapeHtml(trendLabel)}</span>`;

    const mv  = row.movement || {};
    const cur = row.current  || {};
    const prev = row.previous || {};

    const spendMove   = _hiMovementArrow(mv.spend);
    const sqlMove     = _hiMovementArrow(mv.sqls);
    const junkMove    = _hiMovementArrow(mv.junk_rate);
    const cpqlMove    = _hiMovementArrow(mv.cpql);
    const note        = escapeHtml(row.human_review_note || "—");

    if (isGeo) {
      return `<tr>
        <td>${escapeHtml(row.country || "—")}</td>
        <td>${escapeHtml(row.campaign_name || "—")}</td>
        <td>${trendBadge}</td>
        <td>${spendMove}</td>
        <td class="hi-note">${note}</td>
      </tr>`;
    }
    return `<tr>
      <td>${escapeHtml(row.campaign_name || "—")}</td>
      <td>${trendBadge}</td>
      <td>${spendMove}</td>
      <td>${sqlMove}</td>
      <td>${junkMove}</td>
      <td>${cpqlMove}</td>
      <td class="hi-note">${note}</td>
    </tr>`;
  }).join("");

  bodyEl.innerHTML = `
    <div style="overflow-x:auto">
      <table class="data-table">
        <thead><tr>${headerHtml}</tr></thead>
        <tbody>${rowsHtml}</tbody>
      </table>
    </div>`;
}

// ── System Health page ─────────────────────────────────────────────────────

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
  for (const [key, label] of [["latest_daily", "Daily"], ["latest_weekly", "Weekly"], ["latest_monthly", "Monthly"], ["latest_incremental", "Incremental"]]) {
    const run = sched[key];
    const runStatus = run ? run.status : "not_run";
    const rCls = runStatus === "success" ? "war-room-run--ok" : runStatus === "failed" ? "war-room-run--error" : "war-room-run--neutral";
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
    "google_ads_api/keywords":     { page: "keywords",     label: "Keyword Performance" },
    "google_ads_api/geo":          { page: "geo",           label: "Country Performance" },
    "google_ads_api/campaigns":    { page: "campaigns",     label: "Campaigns"    },
    "hubspot/contacts":     { page: "leads",         label: "Lead Quality" },
    "hubspot/deals":        { page: "deals",         label: "Deals"        },
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
        <td class="td--name">${escapeHtml(fmt(row.dataset).replace(/_/g, " "))}</td>
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

let _drawerOpenCampaign = null;  // campaign name string currently shown in drawer, or null
let _drawerPreviousFocus = null; // element to restore focus to on close
let activeCampaignDrawerName = null;
let campaignDrawerRequestId = 0;

function isCurrentCampaignDrawerRequest(campaignName, requestId) {
  if (activeCampaignDrawerName !== campaignName) return false;
  if (requestId !== null && requestId !== campaignDrawerRequestId) return false;
  return true;
}

async function loadCampaignAttributionPreview(campaignName, requestId = null) {
  if (!isCurrentCampaignDrawerRequest(campaignName, requestId)) {
    return { status: "stale", rows: [], summary: null };
  }

  campaignAttributionStatus = "loading";
  campaignAttributionRows = [];

  try {
    const params = new URLSearchParams();
    // Campaign drawer opens from the Campaigns evidence page — follow its window
    // exactly (incl. all_time = no lower bound), never a hidden 365-day downgrade.
    params.set("window", getEvidenceWindow());
    params.set("limit", "5");
    params.set("campaign", campaignName);

    const data = await fetchJSON(`/api/gclid-attribution?${params.toString()}`);

    if (!isCurrentCampaignDrawerRequest(campaignName, requestId)) {
      return { status: "stale", rows: [], summary: null };
    }

    if (data.db_unavailable) {
      campaignAttributionStatus = "db_unavailable";
      campaignAttributionRows = [];
      return { status: campaignAttributionStatus, rows: [], summary: data.summary || null };
    }

    campaignAttributionRows = data.rows || [];
    campaignAttributionStatus = campaignAttributionRows.length ? "ok" : "empty";

    return {
      status: campaignAttributionStatus,
      rows: campaignAttributionRows,
      summary: data.summary || null,
      pagination: data.pagination || null,
    };
  } catch (_) {
    if (!isCurrentCampaignDrawerRequest(campaignName, requestId)) {
      return { status: "stale", rows: [], summary: null };
    }
    campaignAttributionStatus = "error";
    campaignAttributionRows = [];
    return { status: campaignAttributionStatus, rows: [], summary: null };
  }
}

async function loadCampaignAttributionQuality(campaignName, requestId = null) {
  if (!isCurrentCampaignDrawerRequest(campaignName, requestId)) {
    return { status: "stale", data: null };
  }

  campaignAttributionQualityStatus = "loading";
  campaignAttributionQuality = null;

  try {
    const params = new URLSearchParams();
    params.set("window", getEvidenceWindow());
    params.set("campaign", campaignName);

    const data = await fetchJSON(`/api/attribution/quality?${params.toString()}`);

    if (!isCurrentCampaignDrawerRequest(campaignName, requestId)) {
      return { status: "stale", data: null };
    }

    if (data.db_unavailable) {
      campaignAttributionQualityStatus = "db_unavailable";
      campaignAttributionQuality = null;
      return { status: campaignAttributionQualityStatus, data: null };
    }

    campaignAttributionQuality = data;
    campaignAttributionQualityStatus =
      data && data.summary && Number(data.summary.loaded_scope_rows) > 0 ? "ok" : "empty";
    return { status: campaignAttributionQualityStatus, data };
  } catch (_) {
    if (!isCurrentCampaignDrawerRequest(campaignName, requestId)) {
      return { status: "stale", data: null };
    }
    campaignAttributionQualityStatus = "error";
    campaignAttributionQuality = null;
    return { status: campaignAttributionQualityStatus, data: null };
  }
}

async function loadCampaignDrawerNgrams(campaignName, requestId = null) {
  if (!isCurrentCampaignDrawerRequest(campaignName, requestId)) {
    return { status: "stale", rows: [] };
  }

  drawerNgramStatus = "loading";
  drawerNgramRows   = [];

  try {
    const params = new URLSearchParams();
    params.set("window",   getEvidenceWindow());
    params.set("campaign", campaignName);
    params.set("limit",    "20");

    const data = await fetchJSON(`/api/search-terms/ngrams?${params.toString()}`);

    if (!isCurrentCampaignDrawerRequest(campaignName, requestId)) {
      return { status: "stale", rows: [] };
    }

    if (data.db_unavailable) {
      drawerNgramStatus = "db_unavailable";
      drawerNgramRows   = [];
      return { status: drawerNgramStatus, rows: [] };
    }

    drawerNgramRows   = data.rows || [];
    drawerNgramStatus = drawerNgramRows.length ? "ok" : "empty";
    return { status: drawerNgramStatus, rows: drawerNgramRows };
  } catch (_) {
    if (!isCurrentCampaignDrawerRequest(campaignName, requestId)) {
      return { status: "stale", rows: [] };
    }
    drawerNgramStatus = "error";
    drawerNgramRows   = [];
    return { status: drawerNgramStatus, rows: [] };
  }
}


async function openCampaignDrawer(campaignName, campaignKey = null) {
  if (!campaignName) return;
  _drawerOpenCampaign = campaignName;
  activeCampaignDrawerName = campaignName;
  campaignDrawerRequestId += 1;
  const requestId = campaignDrawerRequestId;

  const overlay   = document.getElementById("campaign-drawer-overlay");
  const drawer    = document.getElementById("campaign-drawer");
  const titleEl   = document.getElementById("drawer-campaign-title");
  const bodyEl    = document.getElementById("campaign-drawer-body");

  // Save the currently focused element so we can restore it on close
  _drawerPreviousFocus = document.activeElement;

  // Show overlay + drawer immediately with loading state
  if (overlay) { overlay.hidden = false; overlay.removeAttribute("aria-hidden"); }
  if (drawer)  { drawer.hidden  = false; }
  if (titleEl) titleEl.textContent = campaignName;
  if (bodyEl)  bodyEl.innerHTML = `<p class="empty-state"><span class="spinner"></span> Loading campaign detail…</p>`;

  // Move focus into the drawer — prefer close button, fallback drawer itself
  const closeBtn = document.getElementById("drawer-close-btn");
  if (closeBtn) {
    closeBtn.focus();
  } else if (drawer) {
    drawer.setAttribute("tabindex", "-1");
    drawer.focus();
  }

  // Bind keyboard handlers: Escape closes, Tab traps focus inside drawer
  document.addEventListener("keydown", _drawerKeyHandler);

  try {
    // Campaign detail is primary payload. Attribution preview/quality/ngrams are secondary.
    campaignAttributionStatus = "loading";
    campaignAttributionRows = [];
    campaignAttributionQualityStatus = "loading";
    campaignAttributionQuality = null;
    drawerNgramStatus = "loading";
    drawerNgramRows   = [];

    const keyParam = campaignKey ? `&campaign_key=${encodeURIComponent(campaignKey)}` : "";
    const detail = await fetchJSON(
      `/api/campaign-detail?campaign_name=${encodeURIComponent(campaignName)}${keyParam}&window=${encodeURIComponent(getEvidenceWindow())}`
    );

    if (!isCurrentCampaignDrawerRequest(campaignName, requestId)) return;
    renderCampaignDrawer(detail);

    loadCampaignAttributionPreview(campaignName, requestId)
      .then(() => {
        if (!isCurrentCampaignDrawerRequest(campaignName, requestId)) return;
        renderCampaignDrawer(detail);
      })
      .catch(() => {
        if (!isCurrentCampaignDrawerRequest(campaignName, requestId)) return;
        campaignAttributionStatus = "error";
        campaignAttributionRows = [];
        renderCampaignDrawer(detail);
      });

    loadCampaignAttributionQuality(campaignName, requestId)
      .then(() => {
        if (!isCurrentCampaignDrawerRequest(campaignName, requestId)) return;
        renderCampaignDrawer(detail);
      })
      .catch(() => {
        if (!isCurrentCampaignDrawerRequest(campaignName, requestId)) return;
        campaignAttributionQualityStatus = "error";
        campaignAttributionQuality = null;
        renderCampaignDrawer(detail);
      });

    loadCampaignDrawerNgrams(campaignName, requestId)
      .then(() => {
        if (!isCurrentCampaignDrawerRequest(campaignName, requestId)) return;
        renderCampaignDrawer(detail);
      })
      .catch(() => {
        if (!isCurrentCampaignDrawerRequest(campaignName, requestId)) return;
        drawerNgramStatus = "error";
        drawerNgramRows   = [];
        renderCampaignDrawer(detail);
      });
  } catch (err) {
    if (!isCurrentCampaignDrawerRequest(campaignName, requestId)) return;
    if (bodyEl) {
      bodyEl.innerHTML = `
        <div class="drawer-section">
          <p class="drawer-empty">Could not load campaign detail — ${escapeHtml(err.message || "network error")}.</p>
        </div>`;
    }
  }
}

function closeCampaignDrawer() {
  _drawerOpenCampaign = null;
  activeCampaignDrawerName = null;
  const overlay = document.getElementById("campaign-drawer-overlay");
  const drawer  = document.getElementById("campaign-drawer");
  if (overlay) { overlay.hidden = true; overlay.setAttribute("aria-hidden", "true"); }
  if (drawer)  drawer.hidden = true;
  document.removeEventListener("keydown", _drawerKeyHandler);

  // Restore focus to the element that opened the drawer
  if (_drawerPreviousFocus && typeof _drawerPreviousFocus.focus === "function" && document.contains(_drawerPreviousFocus)) {
    _drawerPreviousFocus.focus();
  }
  _drawerPreviousFocus = null;
}

function _drawerKeyHandler(e) {
  if (e.key === "Escape") {
    closeCampaignDrawer();
    return;
  }
  if (e.key === "Tab") {
    _trapDrawerFocus(e);
  }
}

function _trapDrawerFocus(e) {
  const drawer = document.getElementById("campaign-drawer");
  if (!drawer || drawer.hidden) return;

  const focusable = Array.from(drawer.querySelectorAll(
    'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])'
  )).filter((el) => !el.closest("[hidden]"));

  if (!focusable.length) {
    e.preventDefault();
    drawer.focus();
    return;
  }

  const first = focusable[0];
  const last  = focusable[focusable.length - 1];

  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

function renderCampaignDrawer(data) {
  const titleEl = document.getElementById("drawer-campaign-title");
  const bodyEl  = document.getElementById("campaign-drawer-body");
  if (!bodyEl) return;

  if (data.db_unavailable) {
    bodyEl.innerHTML = `
      <div class="drawer-section">
        <p class="drawer-empty">Campaign detail temporarily unavailable — database offline.</p>
      </div>`;
    return;
  }

  if (!data.campaign) {
    // Headline card absent (no canonical spend + no lead evidence for this window)
    // but supplementary evidence sections may still have data — render what we have.
    const hasEvidence = (data.lead_quality && data.lead_quality.total_leads > 0) ||
                        (data.countries  && data.countries.length  > 0) ||
                        (data.keywords   && data.keywords.length   > 0) ||
                        (data.waste_terms && data.waste_terms.length > 0) ||
                        (data.recent_leads && data.recent_leads.length > 0);

    if (!hasEvidence) {
      bodyEl.innerHTML = `
        <div class="drawer-section">
          <p class="drawer-empty">No campaign detail available for this window.</p>
        </div>`;
      return;
    }
    // Partial render — note the missing headline summary at the top.
    if (titleEl) titleEl.textContent = data.campaign_name || "";
    // Inject a banner and fall through to the evidence sections below
    bodyEl.innerHTML = "";
    const banner = document.createElement("div");
    banner.className = "drawer-section drawer-section--header";
    banner.innerHTML = `<p class="drawer-source-note" style="color:var(--c-warning)">Campaign summary unavailable for this window — related evidence exists.</p>`;
    bodyEl.appendChild(banner);
    // render remaining evidence using partial helpers
    _appendDrawerEvidenceSections(bodyEl, data, null);
    return;
  }

  const camp = data.campaign;
  const lq   = data.lead_quality;
  if (titleEl) titleEl.textContent = camp.campaign_name || data.campaign_name;

  // ── Header summary — factual outcome status for the selected window ───────
  const winLabel = CAMPAIGN_WINDOW_LABELS[camp.window] || camp.window || "selected window";
  const fxNote = (camp.spend_usd == null && camp.fx_complete === false)
    ? " · USD withheld (FX coverage incomplete)" : "";
  const headerHtml = `
    <div class="drawer-section drawer-section--header">
      <div class="drawer-header-meta">
        ${campaignStatusBadge(camp.outcome_status)}
        <span class="drawer-verdict-reason">${escapeHtml(winLabel)} · selected-window evidence</span>
      </div>
      <div class="drawer-source-note">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
        Canonical Google Ads spend reconciled with deduplicated HubSpot lead outcomes for this window${fxNote}
      </div>
      ${(camp.aliases && camp.aliases.length) ? `<p class="drawer-source-note">Approved external aliases: ${camp.aliases.map((a) => escapeHtml(a)).join(", ")}</p>` : ""}
      <p class="drawer-readonly-note">Outcome status is factual, evidence-based and read-only. No Google Ads changes are made.</p>
    </div>`;

  // ── KPI row — selected-window totals (matches the table row exactly) ──────
  const cpqlStr  = camp.confirmed_sqls == null ? "—"
    : (camp.confirmed_sqls === 0 ? "N/A"
    : (camp.cpql_usd != null ? fmtDollar(camp.cpql_usd) : "—"));
  const junkStr  = camp.junk_rate_pct  != null ? camp.junk_rate_pct.toFixed(1) + "%" : "—";
  const spendVal = camp.spend_usd != null ? fmtDollar(camp.spend_usd)
    : (camp.spend_native != null ? `${fmtGbp(camp.spend_native)} ${escapeHtml(camp.spend_currency || "GBP")}`
       : `<span class="detail-unavailable">Unavailable</span>`);
  const spendSub = (camp.spend_usd != null && camp.spend_native != null)
    ? `<div class="drawer-kpi__note">${fmtGbp(camp.spend_native)} ${escapeHtml(camp.spend_currency || "GBP")}</div>` : "";
  const cnt = (n) => n == null ? "—" : fmtCount(n);
  const kpiHtml = `
    <div class="drawer-section">
      <div class="drawer-kpi-grid">
        <div class="drawer-kpi">
          <div class="drawer-kpi__label">Spend</div>
          <div class="drawer-kpi__value">${spendVal}</div>${spendSub}
        </div>
        <div class="drawer-kpi">
          <div class="drawer-kpi__label">Leads</div>
          <div class="drawer-kpi__value">${cnt(camp.total_leads)}</div>
        </div>
        <div class="drawer-kpi">
          <div class="drawer-kpi__label">SQLs</div>
          <div class="drawer-kpi__value">${cnt(camp.confirmed_sqls)}</div>
        </div>
        <div class="drawer-kpi">
          <div class="drawer-kpi__label">Junk</div>
          <div class="drawer-kpi__value">${cnt(camp.confirmed_junk)}</div>
        </div>
        <div class="drawer-kpi">
          <div class="drawer-kpi__label">Junk Rate</div>
          <div class="drawer-kpi__value">${junkStr}</div>
        </div>
        <div class="drawer-kpi">
          <div class="drawer-kpi__label">CPQL</div>
          <div class="drawer-kpi__value">${cpqlStr}</div>
        </div>
      </div>
    </div>`;

  bodyEl.innerHTML = headerHtml + kpiHtml;
  // The Lead Quality Split reads the SAME selected-window card so the drawer split
  // matches the headline and the table exactly; falls back to the campaign-scoped
  // lead query only when the window card carries no lead evidence.
  const lqFromCard = (camp.total_leads != null) ? {
    total_leads: camp.total_leads, confirmed_sqls: camp.confirmed_sqls,
    in_progress: camp.in_progress, confirmed_junk: camp.confirmed_junk,
    wrong_fit: camp.wrong_fit, unknown: camp.unknown,
    verdicted_leads: camp.verdicted_leads, junk_rate_pct: camp.junk_rate_pct,
  } : null;
  _appendDrawerEvidenceSections(bodyEl, data, lqFromCard || lq);
}

/**
 * Appends lead quality, country, keyword, waste, recent leads, and source footer
 * sections to `container`. Shared between full and partial drawer renders.
 * `lq` is the lead_quality object (or null if absent).
 */
function _appendDrawerEvidenceSections(container, data, lq) {
  // ── Lead Quality Split ─────────────────────────────────────────────────
  let lqHtml;
  if (!lq) {
    lqHtml = `
      <div class="drawer-section">
        <div class="drawer-section__title">Lead Quality Split</div>
        <p class="drawer-empty">No lead rows for this campaign in selected window.</p>
      </div>`;
  } else {
    const lqJunkCls = lq.junk_rate_pct == null ? "" :
                      lq.junk_rate_pct < uiThresholds.junk_rate.low_pct   ? "junk--low" :
                      lq.junk_rate_pct <= uiThresholds.junk_rate.high_pct ? "junk--mid" : "junk--high";
    const lqJunkStr = lq.junk_rate_pct != null ? lq.junk_rate_pct.toFixed(1) + "%" : "—";
    lqHtml = `
      <div class="drawer-section">
        <div class="drawer-section__title">Lead Quality Split</div>
        <table class="drawer-table">
          <thead>
            <tr>
              <th>Qualified</th><th>In Progress</th><th>Junk</th><th>Wrong Fit</th><th>Unknown</th><th>Junk Rate</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>${lq.confirmed_sqls}</td>
              <td>${lq.in_progress}</td>
              <td>${lq.confirmed_junk}</td>
              <td>${lq.wrong_fit}</td>
              <td>${lq.unknown}</td>
              <td class="${lqJunkCls}">${lqJunkStr}</td>
            </tr>
          </tbody>
        </table>
        <p class="drawer-source-note">Junk rate = confirmed junk ÷ verdicted leads (qualified + in-progress + junk + wrong fit). Unknown contacts are excluded from the denominator. Total in window: ${lq.total_leads}.</p>
      </div>`;
  }

  // ── Country Breakdown ──────────────────────────────────────────────────
  let countryHtml;
  const countries = data.countries || [];
  if (countries.length === 0) {
    countryHtml = `
      <div class="drawer-section">
        <div class="drawer-section__title">Country Breakdown</div>
        <p class="drawer-empty">No lead rows for this campaign in selected window.</p>
      </div>`;
  } else {
    const rows = countries.map((r) => {
      const junkCls = r.junk_rate_pct == null ? "" :
                      r.junk_rate_pct < uiThresholds.junk_rate.low_pct   ? "junk--low" :
                      r.junk_rate_pct <= uiThresholds.junk_rate.high_pct ? "junk--mid" : "junk--high";
      return `
        <tr>
          <td class="td--name">${escapeHtml(r.country)}</td>
          <td>${r.total_leads}</td>
          <td>${r.confirmed_sqls}</td>
          <td>${r.in_progress != null ? r.in_progress : "—"}</td>
          <td>${r.confirmed_junk}</td>
          <td>${r.wrong_fit}</td>
          <td>${r.unknown}</td>
          <td class="${junkCls}">${r.junk_rate_pct != null ? r.junk_rate_pct.toFixed(1) + "%" : "—"}</td>
        </tr>`;
    }).join("");
    countryHtml = `
      <div class="drawer-section">
        <div class="drawer-section__title">Country Breakdown</div>
        <table class="drawer-table">
          <thead>
            <tr><th>Country</th><th>Leads</th><th>SQLs</th><th>In Progress</th><th>Junk</th><th>Wrong Fit</th><th>Unknown</th><th>Junk Rate</th></tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  // ── Keyword Preview ────────────────────────────────────────────────────
  let kwHtml;
  const keywords = data.keywords || [];
  if (keywords.length === 0) {
    kwHtml = `
      <div class="drawer-section">
        <div class="drawer-section__title">Keyword Preview</div>
        <p class="drawer-empty">No keyword rows for this campaign in selected window.</p>
      </div>`;
  } else {
    const kwRows = keywords.map((k) => `
      <tr>
        <td class="td--name">${escapeHtml(k.keyword || "—")}</td>
        <td>${matchTypeBadge(k.match_type)}</td>
        <td>${k.spend_usd != null ? fmtDollar(k.spend_usd) : "—"}</td>
        <td>${k.clicks != null ? k.clicks : "—"}</td>
        <td>${k.cpc_usd != null ? "$" + k.cpc_usd.toFixed(2) : "—"}</td>
        <td>${k.conversions != null ? k.conversions.toFixed(1) : "—"}</td>
        <td>${qualityScoreBadge(k.quality_score)}</td>
      </tr>`).join("");
    kwHtml = `
      <div class="drawer-section">
        <div class="drawer-section__title">Keyword Preview</div>
        <table class="drawer-table">
          <thead>
            <tr><th>Keyword</th><th>Match</th><th>Spend</th><th>Clicks</th><th>CPC</th><th>Google Conv.</th><th>QS</th></tr>
          </thead>
          <tbody>${kwRows}</tbody>
        </table>
        <p class="drawer-source-note">${escapeHtml(data.keywords_note || "Latest keyword snapshot — not selected-window totals")}. Google Ads API platform metrics only.</p>
        <button class="btn btn--secondary drawer-keywords-fullpage-btn" type="button">Open full Keyword Evidence</button>
      </div>`;
  }

  // ── Waste Terms Preview ────────────────────────────────────────────────
  const wasteTerms = data.waste_terms || [];
  let wasteHtml;
  if (wasteTerms.length === 0) {
    wasteHtml = `
      <div class="drawer-section">
        <div class="drawer-section__title">Waste Terms Preview</div>
        <p class="drawer-empty">No flagged waste terms for this campaign in selected window.</p>
      </div>`;
  } else {
    const wtRows = wasteTerms.map((t) => `
      <tr>
        <td class="td--name waste-pattern">${escapeHtml(t.search_term || "—")}</td>
        <td>${t.spend_usd != null ? fmtDollar(t.spend_usd) : "—"}</td>
        <td>${t.junk_category ? junkCategoryBadge(t.junk_category) : "—"}</td>
        <td class="waste-pattern">${escapeHtml(t.matched_pattern || "—")}</td>
        <td>${t.crm_junk_confirmed != null ? t.crm_junk_confirmed : "—"}</td>
      </tr>`).join("");
    wasteHtml = `
      <div class="drawer-section">
        <div class="drawer-section__title">Waste Terms Preview</div>
        <table class="drawer-table">
          <thead>
            <tr><th>Search Term</th><th>Spend</th><th>Category</th><th>Pattern</th><th>CRM Junk Confirmed</th></tr>
          </thead>
          <tbody>${wtRows}</tbody>
        </table>
        <div style="margin-top:var(--space-3)">
          <button class="btn btn--secondary waste-copy-btn" type="button" id="drawer-waste-copy-btn">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
            Copy waste terms from this campaign
          </button>
          <button class="btn btn--secondary drawer-waste-fullpage-btn" type="button" style="margin-left:var(--space-2)">Open full Waste Evidence</button>
          <p class="drawer-source-note" style="margin-top:var(--space-2)">Review-only — no action controls.</p>
        </div>
      </div>`;
  }

  // ── Recent Leads ───────────────────────────────────────────────────────
  const recentLeads = data.recent_leads || [];
  let leadsHtml;
  if (recentLeads.length === 0) {
    leadsHtml = `
      <div class="drawer-section">
        <div class="drawer-section__title">Recent Leads</div>
        <p class="drawer-empty">No lead rows for this campaign in selected window.</p>
      </div>`;
  } else {
    const rlRows = recentLeads.map((l) => {
      const catCls = l.status_category === "qualified"   ? "junk--low"  :
                     l.status_category === "junk"        ? "junk--high" :
                     l.status_category === "in_progress" ? "junk--mid"  : "";
      return `
        <tr>
          <td class="td--name">${escapeHtml(l.company || "—")}</td>
          <td>${escapeHtml(l.country || "—")}</td>
          <td>${escapeHtml(l.keyword || "—")}</td>
          <td>${escapeHtml(l.mql_status || "—")}</td>
          <td class="${catCls}">${escapeHtml(l.status_category || "—")}</td>
          <td>${escapeHtml(l.run_date || "—")}</td>
        </tr>`;
    }).join("");
    leadsHtml = `
      <div class="drawer-section">
        <div class="drawer-section__title">Recent Leads</div>
        <table class="drawer-table">
          <thead>
            <tr><th>Company</th><th>Country</th><th>Keyword</th><th>MQL Status</th><th>Category</th><th>Run Date</th></tr>
          </thead>
          <tbody>${rlRows}</tbody>
        </table>
      </div>`;
  }

  function renderCampaignAttributionQualityOverlay() {
    const headerHtml = `
      <div class="drawer-attribution-quality">
        <div class="drawer-attribution-quality__title">Attribution Quality</div>
        <p class="drawer-attribution-quality__subtitle">Campaign-scoped evidence quality based on stored GCLID rows.</p>`;

    if (campaignAttributionQualityStatus === "loading" || campaignAttributionQualityStatus === "idle") {
      return `${headerHtml}<p class="drawer-empty">Loading attribution quality…</p></div>`;
    }

    if (campaignAttributionQualityStatus === "db_unavailable") {
      return `${headerHtml}<p class="drawer-empty">Attribution quality temporarily unavailable — database offline.</p></div>`;
    }

    if (campaignAttributionQualityStatus === "error") {
      return `${headerHtml}<p class="drawer-empty">Could not load attribution quality for this campaign.</p></div>`;
    }

    if (campaignAttributionQualityStatus === "empty" || !campaignAttributionQuality) {
      return `${headerHtml}<p class="drawer-empty">No attribution quality signals available for this campaign in the selected window.</p></div>`;
    }

    const qualityData = campaignAttributionQuality || {};
    const rates = qualityData.rates || {};
    const signals = qualityData.signals || [];
    const signalByKey = {};
    signals.forEach((signal) => {
      if (signal && signal.key) signalByKey[String(signal.key)] = signal;
    });

    const cards = [
      { key: "match_strength", label: "Match Strength" },
      { key: "url_fallback_reliance", label: "URL Fallback Reliance" },
      { key: "deal_linkage", label: "Deal Linkage" },
      { key: "freshness", label: "Freshness" },
    ];

    const cardsHtml = cards.map((card) => {
      const sig = signalByKey[card.key] || {};
      const statusCls = _gclidQualityStatusClass(sig.status || "unknown");
      const statusLabel = _gclidQualityStatusLabel(sig.status || "unknown");
      const detail = sig.detail || "No quality detail available for this signal.";
      return `
        <div class="gclid-quality-card drawer-attribution-quality-card ${statusCls}">
          <div class="gclid-quality-card__status">${escapeHtml(statusLabel)}</div>
          <div class="gclid-quality-card__label">${escapeHtml(card.label)}</div>
          <div class="gclid-quality-card__detail">${escapeHtml(detail)}</div>
        </div>`;
    }).join("");

    const fmtPct = (value) => (value == null ? "—" : `${Number(value).toFixed(0)}%`);
    const metricsLine = `Matched ${fmtPct(rates.matched_rate_pct)} · URL fallback ${fmtPct(rates.url_fallback_rate_pct)} · Deal link ${fmtPct(rates.deal_link_rate_pct)} · Amount coverage ${fmtPct(rates.deal_amount_coverage_pct)}`;

    return `
      ${headerHtml}
        <div class="drawer-attribution-quality-grid">${cardsHtml}</div>
        <p class="drawer-attribution-quality-metrics">${escapeHtml(metricsLine)}</p>
      </div>`;
  }

  // ── GCLID Attribution Preview ──────────────────────────────────────────
  let attrHtml;
  if (campaignAttributionStatus === "loading") {
    attrHtml = `
      <div class="drawer-section drawer-attribution-section">
        <div class="drawer-section__title">GCLID Attribution</div>
        <p class="drawer-empty">Loading attribution evidence…</p>
        ${renderCampaignAttributionQualityOverlay()}
      </div>`;
  } else if (campaignAttributionStatus === "db_unavailable") {
    attrHtml = `
      <div class="drawer-section drawer-attribution-section">
        <div class="drawer-section__title">GCLID Attribution</div>
        <p class="drawer-empty">GCLID attribution temporarily unavailable — database offline.</p>
        ${renderCampaignAttributionQualityOverlay()}
      </div>`;
  } else if (campaignAttributionStatus === "error") {
    attrHtml = `
      <div class="drawer-section drawer-attribution-section">
        <div class="drawer-section__title">GCLID Attribution</div>
        <p class="drawer-empty">Could not load GCLID attribution for this campaign.</p>
        ${renderCampaignAttributionQualityOverlay()}
      </div>`;
  } else if (campaignAttributionStatus === "empty" || !campaignAttributionRows.length) {
    attrHtml = `
      <div class="drawer-section drawer-attribution-section">
        <div class="drawer-section__title">GCLID Attribution</div>
        <p class="drawer-empty">No GCLID attribution rows found for this campaign in the selected window.</p>
        <p class="drawer-source-note" style="margin-top:var(--space-3)">Campaign-linked Google click IDs connected to HubSpot contacts/deals.</p>
        ${renderCampaignAttributionQualityOverlay()}
        <div style="margin-top:var(--space-3)">
          <button class="btn btn--secondary drawer-gclid-fullpage-btn" type="button">View full GCLID Attribution page</button>
        </div>
      </div>`;
  } else {
    // Compute preview KPIs from loaded rows
    const attrRows = campaignAttributionRows;
    const attrLoaded    = attrRows.length;
    const attrMatched   = attrRows.filter((r) => (r.match_status || "").toLowerCase() === "matched").length;
    const attrFallback  = attrRows.filter((r) => (r.match_status || "").toLowerCase() === "url_fallback").length;
    const attrUnmatched = attrRows.filter((r) => (r.match_status || "").toLowerCase() === "unmatched").length;
    const attrDealAmt   = attrRows.reduce((s, r) => s + (Number(r.deal_amount_usd) || 0), 0);

    const attrKpiHtml = `
      <div class="drawer-attribution-kpis">
        <div class="drawer-kpi">
          <div class="drawer-kpi__label">Loaded Rows</div>
          <div class="drawer-kpi__value">${attrLoaded}</div>
        </div>
        <div class="drawer-kpi">
          <div class="drawer-kpi__label">Matched Rows</div>
          <div class="drawer-kpi__value">${attrMatched}</div>
        </div>
        <div class="drawer-kpi">
          <div class="drawer-kpi__label">URL Fallback</div>
          <div class="drawer-kpi__value">${attrFallback}</div>
        </div>
        <div class="drawer-kpi">
          <div class="drawer-kpi__label">Unmatched</div>
          <div class="drawer-kpi__value">${attrUnmatched}</div>
        </div>
        <div class="drawer-kpi">
          <div class="drawer-kpi__label">Loaded Deal Amount</div>
          <div class="drawer-kpi__value">${fmtDollar(attrDealAmt)}</div>
        </div>
      </div>`;

    const attrTableRows = attrRows.map((r) => `
      <tr>
        <td>${_gclidMiniStatusBadge(r.match_status)}</td>
        <td class="td--name">${escapeHtml(r.company || "—")}</td>
        <td>${escapeHtml(r.deal_id || "—")}</td>
        <td>${escapeHtml(r.deal_stage_label || r.deal_stage || "—")}</td>
        <td>${r.deal_amount_usd != null ? fmtDollar(r.deal_amount_usd) : "—"}</td>
        <td>${escapeHtml(r.keyword || "—")}</td>
        <td>${escapeHtml(r.search_term || "—")}</td>
        <td>${fmtDate(r.contact_created_at)}</td>
      </tr>`).join("");

    attrHtml = `
      <div class="drawer-section drawer-attribution-section">
        <div class="drawer-section__title">GCLID Attribution</div>
        <p class="drawer-source-note" style="margin-bottom:var(--space-3)">Campaign-linked Google click IDs connected to HubSpot contacts/deals.</p>
        ${attrKpiHtml}
        <div class="drawer-attribution-table" style="margin-top:var(--space-4)">
          <table class="drawer-table">
            <thead>
              <tr>
                <th>Match Status</th>
                <th>Company</th>
                <th>Deal ID</th>
                <th>Deal Stage</th>
                <th>Deal Amount</th>
                <th>Keyword</th>
                <th>Search Term</th>
                <th>Contact Created</th>
              </tr>
            </thead>
            <tbody>${attrTableRows}</tbody>
          </table>
        </div>
        <p class="drawer-source-note" style="margin-top:var(--space-3)">
          Google click evidence · HubSpot deal/contact evidence · Loaded preview values only.
          Evidence context only · read-only view.
        </p>
        ${renderCampaignAttributionQualityOverlay()}
        <div style="margin-top:var(--space-3)">
          <button class="btn btn--secondary drawer-gclid-fullpage-btn" type="button">View full GCLID Attribution page</button>
        </div>
      </div>`;
  }

  // ── N-Gram Drilldown ──────────────────────────────────────────────────
  let ngramDrawerHtml;
  if (drawerNgramStatus === "loading" || drawerNgramStatus === "idle") {
    ngramDrawerHtml = `
      <div class="drawer-section">
        <div class="drawer-section__title">N-Gram Patterns</div>
        <p class="drawer-empty">Loading n-gram patterns…</p>
      </div>`;
  } else if (drawerNgramStatus === "db_unavailable") {
    ngramDrawerHtml = `
      <div class="drawer-section">
        <div class="drawer-section__title">N-Gram Patterns</div>
        <p class="drawer-empty">N-gram analysis temporarily unavailable — database offline.</p>
      </div>`;
  } else if (drawerNgramStatus === "error") {
    ngramDrawerHtml = `
      <div class="drawer-section">
        <div class="drawer-section__title">N-Gram Patterns</div>
        <p class="drawer-empty">Could not load n-gram patterns for this campaign.</p>
      </div>`;
  } else if (drawerNgramStatus === "empty" || !drawerNgramRows.length) {
    ngramDrawerHtml = `
      <div class="drawer-section">
        <div class="drawer-section__title">N-Gram Patterns</div>
        <p class="drawer-empty">No n-gram patterns found for this campaign in the selected window.</p>
        <div style="margin-top:var(--space-3)">
          <button class="btn btn--secondary drawer-ngrams-fullpage-btn" type="button">View N-Gram Intelligence page</button>
        </div>
      </div>`;
  } else {
    const ngramTableRows = drawerNgramRows.map((r) => `
      <tr>
        <td class="td--name">${escapeHtml(r.ngram || "—")}</td>
        <td class="td--num">${r.n != null ? r.n : "—"}</td>
        <td class="td--num">${r.row_count != null ? r.row_count : "—"}</td>
        <td class="td--num">${r.total_spend_usd != null ? fmtDollar(r.total_spend_usd) : "—"}</td>
        <td class="td--num">${r.total_clicks != null ? r.total_clicks : "—"}</td>
        <td>${ngramWasteStateMix(r.flagged_waste_rows, r.clean_rows, r.unanalyzed_rows)}</td>
      </tr>`).join("");
    ngramDrawerHtml = `
      <div class="drawer-section">
        <div class="drawer-section__title">N-Gram Patterns</div>
        <p class="drawer-source-note" style="margin-bottom:var(--space-3)">Top recurring word patterns from this campaign's search terms. Read-only — candidate patterns for human review only.</p>
        <div class="drawer-attribution-table">
          <table class="drawer-table">
            <thead>
              <tr>
                <th>N-Gram</th>
                <th class="td--num">N</th>
                <th class="td--num">Rows</th>
                <th class="td--num">Spend</th>
                <th class="td--num">Clicks</th>
                <th>Flagged / Clean / Unanalyzed</th>
              </tr>
            </thead>
            <tbody>${ngramTableRows}</tbody>
          </table>
        </div>
        <div style="margin-top:var(--space-3)">
          <button class="btn btn--secondary drawer-ngrams-fullpage-btn" type="button">View N-Gram Intelligence page</button>
        </div>
      </div>`;
  }

  // ── Data source footer ─────────────────────────────────────────────────
  const src = data.data_sources || {};
  const footerHtml = `
    <div class="drawer-section drawer-source-footer">
      <p class="drawer-source-note">
        Data sources: ${escapeHtml(src.campaign || "campaigns table")},
        ${escapeHtml(src.lead_quality || "HubSpot-derived leads table")},
        ${escapeHtml(src.keywords || "Google Ads API keyword performance")},
        ${escapeHtml(src.waste_terms || "waste_terms table")}.
      </p>
    </div>`;

  container.insertAdjacentHTML("beforeend", lqHtml + countryHtml + kwHtml + wasteHtml + leadsHtml + attrHtml + ngramDrawerHtml + footerHtml);

  // Wire up waste copy button if present (must happen after DOM insertion)
  const wcBtn = container.querySelector("#drawer-waste-copy-btn");
  if (wcBtn) {
    wcBtn.addEventListener("click", () => copyDrawerWasteTerms(data.waste_terms || [], wcBtn));
  }

  // Wire up "View full GCLID Attribution page" button
  const gclidFullBtn = container.querySelector(".drawer-gclid-fullpage-btn");
  if (gclidFullBtn) {
    const _campName = _drawerOpenCampaign;
    gclidFullBtn.addEventListener("click", () => {
      if (_campName) gclidAttributionPrefill = { campaign: _campName };
      closeCampaignDrawer();
      navigate("gclid-attribution");
    });
  }

  // Wire up "View N-Gram Intelligence page" button
  const ngramsFullBtn = container.querySelector(".drawer-ngrams-fullpage-btn");
  if (ngramsFullBtn) {
    const _campName = _drawerOpenCampaign;
    ngramsFullBtn.addEventListener("click", () => {
      if (_campName) ngramsPrefill = { campaign: _campName };
      closeCampaignDrawer();
      navigate("ngrams");
    });
  }

  // PR-ADS-142: "Open full Keyword / Waste Evidence" — navigate to the dedicated
  // evidence page with the campaign filter prefilled (no duplicate table here).
  const kwFullBtn = container.querySelector(".drawer-keywords-fullpage-btn");
  if (kwFullBtn) {
    const _campName = _drawerOpenCampaign;
    kwFullBtn.addEventListener("click", () => {
      if (_campName) keywordsPrefill = { campaign: _campName };
      closeCampaignDrawer();
      navigate("keywords");
    });
  }
  const wasteFullBtn = container.querySelector(".drawer-waste-fullpage-btn");
  if (wasteFullBtn) {
    const _campName = _drawerOpenCampaign;
    wasteFullBtn.addEventListener("click", () => {
      if (_campName) wastePrefill = { campaign: _campName };
      closeCampaignDrawer();
      navigate("waste");
    });
  }
}

function copyDrawerWasteTerms(terms, btn) {
  const text = terms.map((t) => (t.search_term || "").trim()).filter(Boolean).join("\n");
  if (!text) return;
  const origHTML = btn ? btn.innerHTML : null;
  const showFeedback = (success) => {
    if (!btn) return;
    btn.innerHTML = success
      ? `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg> Copied!`
      : `Failed`;
    btn.disabled = true;
    setTimeout(() => {
      if (btn && origHTML !== null) { btn.innerHTML = origHTML; btn.disabled = false; }
    }, 2000);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => showFeedback(true), () => showFeedback(false));
  } else {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) { /* ignore */ }
    document.body.removeChild(ta);
    showFeedback(ok);
  }
}

// ── Action Queue page ──────────────────────────────────────────────────────

const QUEUE_TYPE_LABELS = {
  campaign_review:     "Campaign",
  waste_review:        "Waste",
  geo_review:          "Geo",
  keyword_review:      "Keyword",
  data_quality_review: "Data Quality",
};

async function loadActionQueue() {
  renderPageDatasetFreshness("action_queue");
  const listEl = document.getElementById("action-queue-list");
  if (listEl) listEl.innerHTML = `<p class="empty-state">Loading action queue…</p>`;

  ["aq-kpi-total", "aq-kpi-high", "aq-kpi-medium", "aq-kpi-low"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.textContent = "—";
  });

  actionQueueStatus = "loading";
  actionQueueItems = [];

  try {
    const data = await fetchJSON(`/api/action-queue?days=${getSelectedDays()}`);

    if (data.db_unavailable) {
      actionQueueStatus = "db_unavailable";
      actionQueueItems = [];
      _renderActionQueueKPIs(null);
      _renderActionQueueList();
      return;
    }

    actionQueueItems = data.items || [];
    actionQueueStatus = actionQueueItems.length === 0 ? "empty" : "ok";
    _renderActionQueueKPIs(data.summary || null);
    _renderActionQueueList();

  } catch (_) {
    actionQueueStatus = "error";
    actionQueueItems = [];
    _renderActionQueueKPIs(null);
    _renderActionQueueList();
  }
}

function _renderActionQueueKPIs(summary) {
  const totalEl  = document.getElementById("aq-kpi-total");
  const highEl   = document.getElementById("aq-kpi-high");
  const mediumEl = document.getElementById("aq-kpi-medium");
  const lowEl    = document.getElementById("aq-kpi-low");

  if (!summary) {
    [totalEl, highEl, mediumEl, lowEl].forEach((el) => {
      if (el) el.textContent = "—";
    });
    return;
  }

  if (totalEl)  totalEl.textContent  = String(summary.total  ?? "—");
  if (highEl)   highEl.textContent   = String(summary.high   ?? "—");
  if (mediumEl) mediumEl.textContent = String(summary.medium ?? "—");
  if (lowEl)    lowEl.textContent    = String(summary.low    ?? "—");
}

function _getFilteredQueueItems() {
  const sevSel  = document.getElementById("aq-filter-severity");
  const typeSel = document.getElementById("aq-filter-type");
  const sevVal  = sevSel  ? sevSel.value  : "";
  const typeVal = typeSel ? typeSel.value : "";

  let items = actionQueueItems;
  if (sevVal)  items = items.filter((i) => i.severity === sevVal);
  if (typeVal) items = items.filter((i) => i.type     === typeVal);
  return items;
}

function _renderActionQueueList() {
  const listEl = document.getElementById("action-queue-list");
  if (!listEl) return;

  if (actionQueueStatus === "db_unavailable") {
    listEl.innerHTML = `
      <div class="action-queue-item action-queue-item--empty">
        <p class="empty-state">Action Queue temporarily unavailable — database offline.</p>
      </div>`;
    return;
  }

  if (actionQueueStatus === "error") {
    listEl.innerHTML = `
      <div class="action-queue-item action-queue-item--empty">
        <p class="empty-state">Could not load Action Queue. Check API health or run status.</p>
      </div>`;
    return;
  }

  const items = _getFilteredQueueItems();

  if (items.length === 0) {
    const isFiltered = (document.getElementById("aq-filter-severity") || {}).value ||
                       (document.getElementById("aq-filter-type")     || {}).value;
    listEl.innerHTML = `
      <div class="action-queue-item action-queue-item--empty">
        <p class="empty-state">${isFiltered
          ? "No items match the current filter."
          : "No review items found for the selected window."
        }</p>
        ${!isFiltered ? `<p class="empty-state-sub">This means no queue rules crossed the current review thresholds.</p>` : ""}
      </div>`;
    return;
  }

  listEl.innerHTML = items.map((item) => _renderQueueItemCard(item)).join("");

  // Wire up action buttons
  listEl.querySelectorAll(".queue-action-btn[data-campaign]").forEach((btn) => {
    btn.addEventListener("click", () => openCampaignDrawer(btn.dataset.campaign));
  });
  listEl.querySelectorAll(".queue-action-btn[data-navigate]").forEach((btn) => {
    btn.addEventListener("click", () => navigate(btn.dataset.navigate));
  });
}

function _renderQueueItemCard(item) {
  const rawSev  = item.severity || "low";
  const rawType = item.type     || "";
  // Sanitize values used in class names against known allowed sets
  const sev   = ["high", "medium", "low"].includes(rawSev) ? rawSev : "low";
  const type  = Object.prototype.hasOwnProperty.call(QUEUE_TYPE_LABELS, rawType) ? rawType : "";
  const score = item.severity_score != null ? item.severity_score : 0;

  const sevBadge  = `<span class="action-queue-badge severity-${escapeHtml(sev)}">${escapeHtml(sev.toUpperCase())}</span>`;
  const typeBadge = `<span class="queue-type-badge queue-type-${escapeHtml(type)}">${escapeHtml(QUEUE_TYPE_LABELS[type] || type)}</span>`;

  // Build evidence mini-grid
  const ev = item.evidence || {};
  const evEntries = Object.entries(ev).filter(([, v]) => v !== null && v !== undefined);
  let evidenceHtml = "";
  if (evEntries.length > 0) {
    const cells = evEntries.map(([k, v]) => {
      const label = k.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
      const display = typeof v === "number"
        ? (k.includes("usd") ? fmtDollar(v) : (k.includes("pct") ? v.toFixed(1) + "%" : String(v)))
        : escapeHtml(String(v));
      return `<div class="queue-evidence-cell"><div class="queue-evidence-cell__label">${escapeHtml(label)}</div><div class="queue-evidence-cell__value">${display}</div></div>`;
    }).join("");
    evidenceHtml = `<div class="queue-evidence-grid">${cells}</div>`;
  }

  // Build read-only action button driven by primary_link contract; fall back to type-based defaults.
  let actionBtn = "";
  const link = item.primary_link || {};
  // Only trust navigation to pages explicitly listed in the PAGES registry.
  const linkAction  = typeof link.action        === "string" ? link.action        : "";
  const linkPage    = typeof link.page          === "string" && PAGES.includes(link.page) ? link.page : "";
  const linkCampaign = typeof link.campaign_name === "string" && link.campaign_name ? link.campaign_name : "";

  if (linkAction === "open_campaign_drawer" && (linkCampaign || item.campaign_name)) {
    const campaignName = linkCampaign || item.campaign_name;
    actionBtn = `<button class="btn btn--secondary queue-action-btn" type="button" data-campaign="${escapeHtml(campaignName)}">Investigate campaign</button>`;
  } else if (linkAction === "navigate" && linkPage) {
    const navLabels = {
      waste: "View Waste Terms", geo: "View Geo",
      keywords: "View Keywords", scheduler: "View Scheduler",
    };
    const label = navLabels[linkPage] || "View";
    actionBtn = `<button class="btn btn--secondary queue-action-btn" type="button" data-navigate="${escapeHtml(linkPage)}">${escapeHtml(label)}</button>`;
  } else if (type === "campaign_review" && item.campaign_name) {
    actionBtn = `<button class="btn btn--secondary queue-action-btn" type="button" data-campaign="${escapeHtml(item.campaign_name)}">Investigate campaign</button>`;
  } else if (type === "waste_review") {
    actionBtn = `<button class="btn btn--secondary queue-action-btn" type="button" data-navigate="waste">View Waste Terms</button>`;
  } else if (type === "geo_review") {
    actionBtn = `<button class="btn btn--secondary queue-action-btn" type="button" data-navigate="geo">View Geo</button>`;
  } else if (type === "keyword_review") {
    actionBtn = `<button class="btn btn--secondary queue-action-btn" type="button" data-navigate="keywords">View Keywords</button>`;
  } else if (type === "data_quality_review") {
    actionBtn = `<button class="btn btn--secondary queue-action-btn" type="button" data-navigate="scheduler">View Scheduler</button>`;
  }

  const sourceHtml = item.source
    ? `<div class="queue-source">Source: ${escapeHtml(item.source)}</div>`
    : "";

  return `
    <div class="action-queue-item severity-border-${escapeHtml(sev)}">
      <div class="action-queue-item__header">
        <div class="action-queue-item__badges">
          ${sevBadge}
          ${typeBadge}
          <span class="queue-score-note">Score: ${score}</span>
        </div>
        ${actionBtn ? `<div class="queue-readonly-actions">${actionBtn}</div>` : ""}
      </div>
      <div class="action-queue-item__title">${escapeHtml(item.title || "")}</div>
      <div class="action-queue-item__detail">${escapeHtml(item.detail || "")}</div>
      ${evidenceHtml}
      ${sourceHtml}
    </div>`;
}

function applyActionQueueFilters() {
  _renderActionQueueList();
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
  const wasteSearch  = document.getElementById("waste-filter-search");
  const wasteCatSel  = document.getElementById("waste-filter-category");
  const wasteCampSel = document.getElementById("waste-filter-campaign");
  const wasteCopyBtn = document.getElementById("waste-copy-btn");
  const wasteExportBtn = document.getElementById("waste-export-csv-btn");
  if (wasteSearch)   wasteSearch.addEventListener("input",  applyWasteFilters);
  if (wasteCatSel)   wasteCatSel.addEventListener("change", applyWasteFilters);
  if (wasteCampSel)  wasteCampSel.addEventListener("change", applyWasteFilters);
  if (wasteCopyBtn)  wasteCopyBtn.addEventListener("click", copyWasteTerms);
  if (wasteExportBtn) wasteExportBtn.addEventListener("click", downloadWasteCSV);

  // Wire up geo search
  const geoSearch = document.getElementById("geo-search");
  if (geoSearch) geoSearch.addEventListener("input", applyGeoSearch);

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

  // Wire up keywords filter controls
  const kwSearch   = document.getElementById("kw-filter-search");
  const kwCampSel  = document.getElementById("kw-filter-campaign");
  const kwMatchSel = document.getElementById("kw-filter-matchtype");
  const kwCopyBtn  = document.getElementById("kw-copy-btn");
  if (kwSearch)   kwSearch.addEventListener("input",   applyKeywordFilters);
  if (kwCampSel)  kwCampSel.addEventListener("change", applyKeywordFilters);
  if (kwMatchSel) kwMatchSel.addEventListener("change", applyKeywordFilters);
  if (kwCopyBtn)  kwCopyBtn.addEventListener("click",  copyKeywordRows);

  // Wire up action queue filter controls
  const aqSevSel  = document.getElementById("aq-filter-severity");
  const aqTypeSel = document.getElementById("aq-filter-type");
  if (aqSevSel)  aqSevSel.addEventListener("change",  applyActionQueueFilters);
  if (aqTypeSel) aqTypeSel.addEventListener("change",  applyActionQueueFilters);

  // Wire up campaign drawer close controls
  const drawerCloseBtn = document.getElementById("drawer-close-btn");
  const drawerOverlay  = document.getElementById("campaign-drawer-overlay");
  if (drawerCloseBtn) drawerCloseBtn.addEventListener("click", closeCampaignDrawer);
  if (drawerOverlay)  drawerOverlay.addEventListener("click", closeCampaignDrawer);

  // Wire up reports page controls
  const reportCopyBtn    = document.getElementById("report-copy-btn");
  const reportRefreshBtn = document.getElementById("report-refresh-btn");
  if (reportCopyBtn)    reportCopyBtn.addEventListener("click", copyLatestReport);
  if (reportRefreshBtn) reportRefreshBtn.addEventListener("click", loadReports);

  // Wire up dataset freshness refresh button
  const dfRefreshBtn = document.getElementById("dataset-freshness-refresh-btn");
  if (dfRefreshBtn) dfRefreshBtn.addEventListener("click", loadDatasetFreshness);

  // Wire up backfill form
  const backfillForm = document.getElementById("backfill-form");
  if (backfillForm) backfillForm.addEventListener("submit", runBackfillFromUI);

  // Wire up historical intelligence controls
  const hiRefreshBtn = document.getElementById("hi-refresh-btn");
  if (hiRefreshBtn) hiRefreshBtn.addEventListener("click", loadHistoricalIntelligence);
  const hiEntitySel   = document.getElementById("hi-entity-select");
  const hiCurSel      = document.getElementById("hi-current-days-select");
  const hiPrevSel     = document.getElementById("hi-previous-days-select");
  if (hiEntitySel)  hiEntitySel.addEventListener("change", loadHistoricalIntelligence);
  if (hiCurSel)     hiCurSel.addEventListener("change",    loadHistoricalIntelligence);
  if (hiPrevSel)    hiPrevSel.addEventListener("change",   loadHistoricalIntelligence);

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

  // Wire up ROAS by Campaign controls (business windows — PR-ADS-107A)
  const roasCampRefresh = document.getElementById("roas-campaigns-refresh-btn");
  const roasCampWindow  = document.getElementById("roas-campaigns-window");
  if (roasCampRefresh) roasCampRefresh.addEventListener("click", loadRoasCampaigns);
  if (roasCampWindow) {
    roasCampWindow.value = getRoasBusinessWindow();
    roasCampWindow.addEventListener("change", handleBusinessWindowSelectChange);
  }

  // Wire up ROAS by Country controls (business windows — PR-ADS-107A)
  const roasCountryRefresh = document.getElementById("roas-countries-refresh-btn");
  const roasCountryWindow  = document.getElementById("roas-countries-window");
  if (roasCountryRefresh) roasCountryRefresh.addEventListener("click", loadRoasCountries);
  if (roasCountryWindow) {
    roasCountryWindow.value = getRoasBusinessWindow();
    roasCountryWindow.addEventListener("change", handleBusinessWindowSelectChange);
  }


  // Wire up Closed-Won Deals controls (business windows — PR-ADS-113)
  const dealsRefresh = document.getElementById("revenue-deals-refresh-btn");
  const dealsWindow  = document.getElementById("revenue-deals-window");
  if (dealsRefresh) dealsRefresh.addEventListener("click", loadDeals);
  if (dealsWindow) {
    dealsWindow.value = getRoasBusinessWindow();
    dealsWindow.addEventListener("change", handleBusinessWindowSelectChange);
  }

  // Wire up Revenue Attribution Health controls (business windows — PR-ADS-113)
  const revHealthRefresh = document.getElementById("revenue-health-refresh-btn");
  const revHealthWindow  = document.getElementById("revenue-health-window");
  if (revHealthRefresh) revHealthRefresh.addEventListener("click", loadRevenueHealth);
  if (revHealthWindow) {
    revHealthWindow.value = getRoasBusinessWindow();
    revHealthWindow.addEventListener("change", handleBusinessWindowSelectChange);
  }

  // Wire up Revenue by Source controls (business windows — PR-ADS-117)
  const bySourceRefresh = document.getElementById("revenue-by-source-refresh-btn");
  const bySourceWindow  = document.getElementById("revenue-by-source-window");
  if (bySourceRefresh) bySourceRefresh.addEventListener("click", loadRevenueBySource);
  if (bySourceWindow) {
    bySourceWindow.value = getRoasBusinessWindow();
    bySourceWindow.addEventListener("change", handleBusinessWindowSelectChange);
  }

  // Wire up Unit Economics controls
  const ueRefresh = document.getElementById("unit-economics-refresh-btn");
  const ueWindow  = document.getElementById("unit-economics-window");
  if (ueRefresh) ueRefresh.addEventListener("click", loadUnitEconomics);
  if (ueWindow)  ueWindow.addEventListener("change", handleSyncedWindowSelectChange);

  // Wire up Churn Input form
  const churnForm = document.getElementById("churn-input-form");
  if (churnForm) churnForm.addEventListener("submit", handleChurnInputSubmit);

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
          <div class="readiness-card__value">${summary.won_deals || 0}</div>
        </div>
        <div class="readiness-card">
          <div class="readiness-card__label">Deals with GCLID</div>
          <div class="readiness-card__value">${(summary.deals_with_direct_gclid || 0) + (summary.deals_with_contact_gclid || 0)}</div>
        </div>
        <div class="readiness-card">
          <div class="readiness-card__label">Windsor GCLID Coverage</div>
          <div class="readiness-card__value">${summary.windsor_rows_with_gclid || 0} rows</div>
        </div>
        <div class="readiness-card">
          <div class="readiness-card__label">Exact Matches Possible</div>
          <div class="readiness-card__value">${summary.tier_1_possible_matches || 0}</div>
        </div>
        <div class="readiness-card">
          <div class="readiness-card__label">Source Tag Matches</div>
          <div class="readiness-card__value">${summary.tier_2_source_tag_matches || 0}</div>
        </div>
        <div class="readiness-card">
          <div class="readiness-card__label">Estimated Rows</div>
          <div class="readiness-card__value">${summary.tier_3_estimated_required || 0}</div>
        </div>
      </div>
      <p class="readiness-note">${status === "READY" ? "GCLID bridge is ready for exact attribution." : "GCLID bridge is not fully wired yet. Current ROAS remains partly directional."}</p>
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

async function loadAttributionConfidenceSummary() {
  const container = document.getElementById("attribution-confidence-summary");
  if (!container) return;

  container.innerHTML = '<p class="empty-state">Loading confidence summary…</p>';

  try {
    const data = await fetchJSON(`/api/attribution/confidence-summary?window=${getCurrentWindowParam()}`);
    if (!data || data.error) {
      container.innerHTML = '<p class="empty-state">Could not load attribution confidence.</p>';
      return;
    }

    const overall = data.overall_confidence || "UNKNOWN";
    const summary = data.summary || {};
    const message = data.message || "";

    const overallCls = overall === "HIGH" ? "confidence-level--high"
      : overall === "MEDIUM" ? "confidence-level--medium"
      : overall === "LOW" ? "confidence-level--low"
      : "confidence-level--unknown";

    const t1Pct = ((summary.tier_1_share || 0) * 100).toFixed(1);
    const t2Pct = ((summary.tier_2_share || 0) * 100).toFixed(1);
    const t3Pct = ((summary.tier_3_share || 0) * 100).toFixed(1);

    container.innerHTML = `
      <div class="confidence-cards">
        <div class="confidence-card">
          <div class="confidence-card__label">Overall Confidence</div>
          <div class="confidence-card__value"><span class="confidence-level-badge ${overallCls}">${escapeHtml(overall)}</span></div>
        </div>
        <div class="confidence-card">
          <div class="confidence-card__label">Campaign Rows</div>
          <div class="confidence-card__value">${summary.campaign_rows || 0}</div>
        </div>
        <div class="confidence-card">
          <div class="confidence-card__label">Country Rows</div>
          <div class="confidence-card__value">${summary.country_rows || 0}</div>
        </div>
        <div class="confidence-card confidence-card--tier1">
          <div class="confidence-card__label"><span class="attribution-badge attribution-badge--tier1">Exact GCLID</span></div>
          <div class="confidence-card__value">${summary.tier_1_count || 0} <small>(${t1Pct}%)</small></div>
          <div class="confidence-card__desc">Click-level match</div>
        </div>
        <div class="confidence-card confidence-card--tier2">
          <div class="confidence-card__label"><span class="attribution-badge attribution-badge--tier2">Source Tag</span></div>
          <div class="confidence-card__value">${summary.tier_2_count || 0} <small>(${t2Pct}%)</small></div>
          <div class="confidence-card__desc">HubSpot paid-search/campaign tag</div>
        </div>
        <div class="confidence-card confidence-card--tier3">
          <div class="confidence-card__label"><span class="attribution-badge attribution-badge--tier3">Estimate</span></div>
          <div class="confidence-card__value">${summary.tier_3_count || 0} <small>(${t3Pct}%)</small></div>
          <div class="confidence-card__desc">Fallback allocation — directional only</div>
        </div>
      </div>
      <p class="confidence-message">${escapeHtml(message)}</p>
    `;
  } catch (err) {
    console.error("[loadAttributionConfidenceSummary]", err);
    container.innerHTML = '<p class="empty-state">Error loading confidence summary.</p>';
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

// ── Business revenue windows (PR-ADS-107A) ────────────────────────────────
// ROAS by Campaign / ROAS by Country use business windows (not ad-style
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
    case "roas-countries":  loadRoasCountries(); break;
    case "deals":           loadDeals();         break;
    case "revenue-health":  loadRevenueHealth(); break;
    case "revenue-by-source": loadRevenueBySource(); break;
    default:                loadRoasCampaigns();
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

function getBusinessVerdictBadge(verdict) {
  switch ((verdict || "").toLowerCase()) {
    case "winner":   return '<span class="roas-verdict-badge roas-verdict-badge--scale" title="Revenue/customers exist and ROAS is healthy">Winner</span>';
    case "watch":    return '<span class="roas-verdict-badge roas-verdict-badge--hold" title="SQLs exist but weak customers/revenue">Watch</span>';
    case "waste":    return '<span class="roas-verdict-badge roas-verdict-badge--cut" title="Spend exists but no SQLs/customers/revenue">Waste</span>';
    case "learning": return '<span class="roas-verdict-badge roas-verdict-badge--insufficient" title="Low spend or insufficient data">Learning</span>';
    // PR-ADS-120: an unmapped campaign cannot be judged on incomplete spend.
    case "mapping_required": return '<span class="roas-verdict-badge roas-verdict-badge--mapping" title="Spend mapping required before this campaign can be judged">Mapping required</span>';
    default:         return '<span class="roas-verdict-badge">' + escapeHtml(verdict || "—") + '</span>';
  }
}

const BUSINESS_VERDICT_ORDER = { winner: 0, watch: 1, waste: 2, learning: 3 };

// PR-ADS-116: every revenue page keeps its OWN window/summary/source-health so a
// Current-Quarter payload can never bleed into another page or another window.
let roasCampaignWindow = null;
let roasCampaignSummary = null;
let roasCampaignSourceHealth = null;
let roasCountryWindow = null;

// Per-page request sequence tokens guard against stale-response races: only the
// newest in-flight request for a page may render. Switching CQ→YTD→Last Quarter
// quickly must never paint an older response over the newest selection.
const _revReqSeq = { campaigns: 0, countries: 0, deals: 0, health: 0, bySource: 0, overview: 0, dashRevenue: 0, dashChannels: 0, dashCampaigns: 0, dashCountries: 0, dashDeals: 0 };

// PR-ADS-116: show the exact resolved date range beside each window selector.
function formatWindowRange(win) {
  if (!win) return "";
  const start = win.start_date || "Earliest";
  const end = win.end_date || "—";
  return `${start} → ${end}`;
}

function renderWindowRange(elId, win) {
  const el = document.getElementById(elId);
  if (!el) return;
  if (!win) { el.textContent = ""; el.removeAttribute("title"); return; }
  const label = win.label ? `${win.label}: ` : "";
  el.textContent = `${label}${formatWindowRange(win)}`;
  el.title = win.is_closed_window ? "Completed period" : "Period in progress";
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


// PR-ADS-110: ROAS by Campaign / Country are business decision pages, not
// diagnostics pages. Decisions are shown only when the truth layer is safe;
// otherwise a single clean blocked card explains why — no debug/audit clutter.
// (Diagnostics live in System Status / Revenue Attribution Health.)
// PR-ADS-115: revenue truth is two independent facts. Readiness requires safe
// lead event dates AND a connected revenue integration. A connected integration
// whose selected window has no closed-won deals is a SAFE EMPTY state (handled
// downstream), NOT a block.
function isRevenueIntegrationConnected(sh) {
  const s = sh || {};
  if (s.revenue_integration_status) return s.revenue_integration_status === "connected";
  // Backward-compatible fallback for older payloads.
  return s.revenue_attribution_status !== "not_wired_or_no_closed_won";
}

function isRevenueDecisionReady(sourceHealth) {
  const sh = sourceHealth || {};
  return (
    sh.lead_date_grain_status === "event_date" &&
    sh.lead_metrics_status !== "withheld" &&
    isRevenueIntegrationConnected(sh)
  );
}

function renderRevenueBlockedState(sourceHealth) {
  const sh = sourceHealth || {};
  const leadsReady = sh.lead_date_grain_status === "event_date" && sh.lead_metrics_status !== "withheld";
  const revenueReady = isRevenueIntegrationConnected(sh);
  const missing = sh.missing_contact_created_at_count;
  const chip = (label, ok, okText, warnText) =>
    `<div class="revenue-status-chip revenue-status-chip--${ok ? "ok" : "warning"}">
       <span>${label}</span><strong>${ok ? okText : warnText}</strong>
     </div>`;
  // The next action points at the real Revenue Health reconciliation workflow —
  // there is no generic lead re-import admin action.
  const nextStep = !leadsReady
    ? `<strong>Next step:</strong> Open <strong>System Status → Revenue Health</strong> and run <strong>Lead Event-Date Reconciliation</strong>${(missing || missing === 0) ? ` (${fmtCount(missing)} included paid leads still missing an event date)` : ""}.`
    : `<strong>Next step:</strong> Connect revenue attribution by running <strong>Revenue Truth Recovery</strong> in <strong>System Status → Revenue Health</strong>.`;
  return `
    <div class="revenue-blocked-card">
      <div class="revenue-blocked-card__eyebrow">Revenue attribution</div>
      <h3>Revenue ROAS is not ready yet</h3>
      <p>This page is blocked because the data is not safe enough for business decisions.</p>
      <div class="revenue-status-grid">
        ${chip("Spend", true, "Available", "Available")}
        ${chip("Leads / SQLs", leadsReady, "Available", "Rebuilding")}
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
let roasCampaignMart = null;
let roasCountryMart = null;
let revenueDealsMart = null;
// PR-ADS-130: geo reconciliation root-cause for the blocked ROAS by Country card.
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

// Canonical Spend Truth card (PR-ADS-128): one shared, clean component across
// Campaign / Country / Source / Deals. Renders ONLY mart fields — never computes
// or fabricates anything. Native spend uses the native-currency formatter (never
// "$" on GBP); USD reporting and the ROAS basis show "Unavailable" (never $0)
// when the mart withholds them. The ROAS magnitude lives in the summary strip;
// this card states the ROAS *basis* so the denominator is never ambiguous.
function renderMartSpendTruth(data) {
  const st = (data && data.spend_truth) || {};
  const s = (data && data.summary) || {};
  return `
    <section class="revenue-spend-card revenue-spend-summary">
      <div class="revenue-spend-card__title revenue-spend-summary__title">Canonical Spend Truth</div>
      <div class="revenue-spend-card__grid revenue-spend-summary__grid">
        <div><span>Native Spend</span><strong>${fmtNativeMoney(st.native_spend, st.native_currency)}</strong></div>
        <div><span>USD Reporting</span><strong>${st.usd_spend == null ? "Unavailable" : fmtMoney(st.usd_spend)}</strong></div>
        <div><span>FX Coverage</span><strong>${labelizeStatus(st.fx_status)}</strong></div>
        <div><span>ROAS Basis</span><strong>${s.roas == null ? "Unavailable" : "USD reporting spend"}</strong></div>
      </div>
    </section>
  `;
}

// Business KPI strip from the canonical summary (identical across every view).
function renderMartSummaryStrip(data) {
  const s = (data && data.summary) || {};
  return `
    <div class="revenue-summary-strip">
      <div><span>Spend (USD)</span><strong>${s.spend_usd == null ? "Unavailable" : fmtMoney(s.spend_usd)}</strong></div>
      <div><span>Leads</span><strong>${fmtCount(s.leads)}</strong></div>
      <div><span>SQLs</span><strong>${fmtCount(s.sqls)}</strong></div>
      <div><span>Customers</span><strong>${fmtCount(s.customers)}</strong></div>
      <div><span>Won Revenue</span><strong>${s.won_revenue_usd == null ? "Unavailable" : fmtMoney(s.won_revenue_usd)}</strong></div>
      <div><span>ROAS</span><strong>${s.roas == null ? "Unavailable" : fmtRoasMultiple(s.roas)}</strong></div>
    </div>
  `;
}

// Compact diagnostics — the mart's own explanation of why spend/ROAS are
// (un)available. One tidy stack, never a warning wall.
function renderMartDiagnostics(data) {
  const diagnostics = (data && data.diagnostics) || [];
  if (!diagnostics.length) return "";
  return `
    <div class="revenue-diagnostics">
      ${diagnostics.map((d) => `
        <div class="revenue-diagnostic">
          <strong>${escapeHtml((d && d.code) || "diagnostic")}</strong>
          <span>${escapeHtml((d && d.message) || "")}</span>
        </div>
      `).join("")}
    </div>
  `;
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
    revenue_integration_status: r.revenue_integration_status,
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
    revenue_integration_status: r.revenue_integration_status,
    revenue_attribution_status: r.revenue_attribution_status,
    missing_contact_created_at_count: r.missing_contact_created_at_count,
    country_spend_available: r.country_spend_available === true,
    geo_country_mapping_status: r.geo_country_mapping_status || "no_geo_data",
  };
}

// ── ROAS by Campaign (PR-ADS-111) ─────────────────────────────────────────
// Executive decision page: "Which Google Ads campaigns are producing qualified
// pipeline, customers, and closed-won revenue?" — not a diagnostics page.
// Blocked card when the truth layer is unsafe; otherwise a summary strip,
// decision filter chips, and a single clean decision table. No debug clutter.

let roasCampaignsData = [];
let roasCampaignsStatus = "idle";

// Frontend-only decision-bucket filter (does not affect ROAS by Country).
let roasCampaignFilter = "all";

// Executive ROAS notation for the Campaign page: a multiple (2.20x), not a
// percent. "Unavailable" when null. Campaign-only — ROAS by Country still
// uses fmtRoas() until PR-ADS-112.
function fmtRoasMultiple(v) {
  if (v === null || v === undefined) return "Unavailable";
  return v.toFixed(2) + "x";
}

function campaignDecisionLabel(verdict) {
  const labels = {
    winner: "Winner",
    watch: "Watch",
    waste: "Waste",
    learning: "Learning",
  };
  return labels[verdict] || "Learning";
}

function filterRoasCampaignRows(rows, filter) {
  if (!filter || filter === "all") return rows;
  return rows.filter((r) => r.verdict === filter);
}

// Business-value sort: a campaign with customers and revenue matters more than
// a prettier verdict label, so we sort by outcome, not by verdict bucket.
function sortRoasCampaignRows(rows) {
  return [...rows].sort((a, b) =>
    (b.customers || 0) - (a.customers || 0) ||
    (b.won_revenue || 0) - (a.won_revenue || 0) ||
    (b.sqls || 0) - (a.sqls || 0) ||
    (b.spend || 0) - (a.spend || 0)
  );
}

function renderRoasCampaignSummary(summary, spendIncomplete) {
  const s = summary || {};
  const sh = roasCampaignSourceHealth || {};
  const roasCell = spendIncomplete ? "Unavailable" : fmtRoasMultiple(s.roas);
  // PR-ADS-119: Google Ads native spend is GBP; USD ROAS uses daily-FX reporting
  // spend. Never render native GBP with "$".
  const native = sh.spend_native_total;
  const usd = sh.spend_usd_total;
  const nativeCur = sh.spend_native_currency || "GBP";
  const fxComplete = sh.fx_coverage_status === "complete";
  const hasNative = native !== null && native !== undefined;
  // PR-ADS-123: a compact, contained summary strip. Exact full-precision values
  // live in the spend proof modal; this card shows magnitudes so it fits cleanly
  // and never wraps a "$93,875.53" onto a stray "USD" line.
  const spendBlock = hasNative ? `
    <div class="revenue-spend-summary">
      <div class="revenue-spend-summary__title">Google Ads Spend</div>
      <div class="revenue-spend-summary__grid">
        <div><span>Native</span><strong title="${fmtCurrency(native, nativeCur)}">${fmtCompactCurrency(native, nativeCur)}</strong></div>
        <div><span>Reporting</span><strong title="${usd === null || usd === undefined ? "" : fmtUSD(usd)}">${usd === null || usd === undefined ? "FX incomplete" : fmtCompactCurrency(usd, "USD")}</strong></div>
        <div><span>FX</span><strong>${fxComplete ? "Verified" : "Incomplete"}</strong></div>
        <div><span>ROAS</span><strong>${spendIncomplete ? "Unavailable" : "USD reporting spend"}</strong></div>
      </div>
    </div>` : "";
  // KPI strip Spend cell: compact USD reporting when FX complete; otherwise a
  // compact native diagnostic (never "$" on native GBP).
  const spendCell = (usd !== null && usd !== undefined)
    ? fmtMoney(usd)
    : (hasNative ? fmtCompactCurrency(native, nativeCur) : fmtMoney(s.spend));
  return `
    ${spendBlock}
    <div class="revenue-summary-strip">
      <div><span>Spend</span><strong>${spendCell}</strong></div>
      <div><span>Leads</span><strong>${fmtCount(s.leads)}</strong></div>
      <div><span>SQLs</span><strong>${fmtCount(s.sqls)}</strong></div>
      <div><span>Customers</span><strong>${fmtCount(s.customers)}</strong></div>
      <div><span>Won Revenue</span><strong>${fmtMoney(s.won_revenue)}</strong></div>
      <div><span>ROAS</span><strong>${roasCell}</strong></div>
    </div>
  `;
}

function renderRoasCampaignFilters() {
  const filters = [
    ["all", "All"],
    ["winner", "Winners"],
    ["watch", "Watch"],
    ["waste", "Waste"],
    ["learning", "Learning"],
  ];
  return `
    <div class="revenue-filter-chips" id="roas-campaign-filter-chips">
      ${filters.map(([key, label]) => `
        <button
          type="button"
          class="revenue-filter-chip ${roasCampaignFilter === key ? "is-active" : ""}"
          data-roas-campaign-filter="${key}"
        >${label}</button>
      `).join("")}
    </div>
  `;
}

function renderRoasCampaignSafeEmptyState() {
  return `
    <div class="revenue-blocked-card revenue-empty-card">
      <div class="revenue-blocked-card__eyebrow">Revenue attribution</div>
      <h3>No campaign revenue found for this window</h3>
      <p>Spend and revenue sources are ready, but no HubSpot closed-won deals are attributed to campaigns in this business window.</p>
    </div>`;
}

async function loadRoasCampaigns() {
  const window_ = getRoasBusinessWindow();
  const token = ++_revReqSeq.campaigns;
  setWindowRangeLoading("roas-campaigns-range");

  roasCampaignsStatus = "loading";
  const body = document.getElementById("roas-campaigns-table-body");
  if (body) body.innerHTML = '<p class="empty-state" style="padding:var(--space-5)">Loading revenue attribution by campaign…</p>';

  try {
    // PR-ADS-126: PRIMARY truth is the canonical mart, not /api/revenue-attribution.
    const data = await fetchRevenuePerformance("campaign", window_);
    // Drop stale responses: a newer selection (or page reload) supersedes this.
    if (token !== _revReqSeq.campaigns) return;
    if (!data) {
      // Mart endpoint failed — NO silent fallback to old endpoints (page drift).
      roasCampaignMart = null;
      roasCampaignsStatus = "error";
      if (body) body.innerHTML = renderMartUnavailable();
      return;
    }
    if (!_revResponseIsCurrent("campaigns", token, data)) return;
    roasCampaignMart = data;
    roasCampaignWindow = data.window || null;
    roasCampaignSummary = data.summary || null;
    roasCampaignSourceHealth = martCampaignSourceHealth(data);
    roasCampaignsData = data.rows || [];
    roasCampaignsStatus = roasCampaignsData.length ? "ok" : "empty";
    renderWindowRange("roas-campaigns-range", roasCampaignWindow);
    renderRoasCampaignsPage();
  } catch (err) {
    console.error("[loadRoasCampaigns]", err);
    if (token !== _revReqSeq.campaigns) return;
    roasCampaignMart = null;
    roasCampaignsStatus = "error";
    if (body) body.innerHTML = renderMartUnavailable();
  }
}

// PR-ADS-118 spend-coverage helpers. Campaign ROAS is available ONLY when the
// backend confirms a complete canonical denominator; geo-fallback and any
// incomplete coverage block ROAS (it is never derived from a partial/geo total).
function spendCoverageIncomplete(sh) {
  const h = sh || {};
  if (h.campaign_roas_available === true) return false;
  if (h.campaign_roas_available === false) return true;
  // Backward-compatible fallback when the explicit flag is absent.
  return h.spend_coverage_status !== "complete";
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
// canonical campaign is shown once (spend never duplicated) with its aliases
// listed underneath.
function campaignAliasHtml(aliases) {
  if (!aliases || !aliases.length) return "";
  return `<div class="campaign-aliases">Aliases: ${escapeHtml(aliases.join("; "))}</div>`;
}

function renderSpendCoverageNotice(sh) {
  const h = sh || {};
  // PR-ADS-119: when spend coverage is complete but FX coverage is not, the
  // blocker is currency conversion — be explicit so it isn't mistaken for a
  // missing-spend problem.
  if (h.spend_coverage_status === "complete" && h.fx_coverage_status === "incomplete") {
    return `
    <div class="revenue-blocked-card revenue-empty-card">
      <div class="revenue-blocked-card__eyebrow">Currency normalization</div>
      <h3>FX coverage incomplete — ROAS unavailable</h3>
      <p>Google Ads native spend is GBP and HubSpot revenue is USD. One or more spend dates have no daily FX rate, so USD reporting spend cannot be computed for the whole window. Run the FX backfill in System Status → Revenue Health. Native spend is shown for diagnostics only.</p>
    </div>`;
  }
  return `
    <div class="revenue-blocked-card revenue-empty-card">
      <div class="revenue-blocked-card__eyebrow">Google Ads spend truth</div>
      <h3>Spend coverage incomplete — ROAS unavailable</h3>
      <p>Canonical Google Ads spend for this window has missing or unverified date chunks, so ROAS would use a partial denominator. Run the Google Ads spend backfill in System Status → Revenue Health. Observed partial spend is shown below for diagnostics only.</p>
    </div>`;
}

function renderRoasCampaignsPage() {
  const kpiGrid = document.getElementById("roas-campaigns-kpis");
  const tableBody = document.getElementById("roas-campaigns-table-body");
  if (kpiGrid) kpiGrid.innerHTML = "";
  if (!tableBody) return;

  // PR-ADS-126: the mart is the only truth. No mart payload → say so, never fall
  // back to a page-specific interpretation.
  const data = roasCampaignMart;
  if (!data) { tableBody.innerHTML = renderMartUnavailable(); return; }

  // Blocked: the MART says revenue truth is not decision-ready (lead grain unsafe
  // or revenue integration not connected). The frontend does not decide this.
  if (!data.readiness || data.readiness.revenue_decision_ready !== true) {
    tableBody.innerHTML = renderRevenueBlockedState(roasCampaignSourceHealth);
    return;
  }

  // Safe but no closed-won revenue rows attributed to campaigns.
  if (!roasCampaignsData.length) {
    tableBody.innerHTML = renderRoasCampaignSafeEmptyState();
    return;
  }

  const sorted = sortRoasCampaignRows(roasCampaignsData);
  const filtered = filterRoasCampaignRows(sorted, roasCampaignFilter);
  // Spend safety is the mart's verdict: when the canonical denominator is unsafe
  // (incomplete coverage / incomplete FX), per-row ROAS is already null and the
  // table renders "Unavailable" — never a partial/geo denominator.
  const spendIncomplete = !martSpendReady(data);

  tableBody.innerHTML = `
    ${renderMartSpendTruth(data)}
    ${renderMartCampaignCoverage(data)}
    ${renderMartSummaryStrip(data)}
    ${renderMartDiagnostics(data)}
    ${renderRoasCampaignFilters()}
    ${renderRoasCampaignTable(filtered, spendIncomplete)}
    <p class="revenue-footnote">Canonical Revenue Decision Mart — Google Ads canonical spend powers ROAS; HubSpot closed-won deals provide revenue.</p>
  `;
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
        <p>${escapeHtml(detail.requested_start || "—")} → ${escapeHtml(detail.first_spend_date || "—")} was verified as zero spend. ROAS uses verified spend for this window.</p>
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

function renderRoasCampaignTable(rows, spendIncomplete) {
  if (!rows.length) {
    return `
      <div class="revenue-empty-inline">
        No campaigns match this decision filter.
      </div>
    `;
  }

  const headerRow = `
    <tr>
      <th>Campaign</th>
      <th>Spend</th>
      <th>Leads</th>
      <th>SQLs</th>
      <th>Customers</th>
      <th>Won Revenue</th>
      <th>ROAS</th>
      <th>Decision</th>
    </tr>
  `;

  // PR-ADS-120: the spend cell shows actual spend (USD when FX complete, else
  // native GBP — never $ for native), with a compact spend-status label. An
  // unmapped campaign is "Unavailable" (never $0) with Decision "Mapping required".
  const sh = roasCampaignSourceHealth || {};
  const usdReady = sh.campaign_roas_available === true;
  const nativeCur = sh.spend_native_currency || "GBP";

  const bodyRows = rows.map((r, i) => {
    const state = r.spend_state;
    const unmapped = state === "unmapped" || r.spend_mapping === "unavailable";
    let spendCell;
    let roasCell;
    let decisionCell;
    if (unmapped) {
      spendCell = '<span class="spend-unavailable" title="Spend mapping required">Unavailable</span>'
                + '<div class="spend-status spend-status--unmapped">Spend mapping required</div>';
      roasCell = "Unavailable";
      decisionCell = getBusinessVerdictBadge("mapping_required");
    } else {
      const amt = usdReady ? fmtMoney(r.spend) : fmtCurrency(r.spend, nativeCur);
      spendCell = `${amt}${spendStatusLabel(state)}`;
      roasCell = spendIncomplete ? "Unavailable" : fmtRoasMultiple(r.roas);
      decisionCell = getBusinessVerdictBadge(r.verdict);
    }
    // PR-ADS-130: an expand control opens a per-campaign drawer of the closed-won
    // clients/deals behind the revenue (lazy-loaded on first open).
    const expandLabel = `Show clients / deals behind ${r.campaign_name || "this campaign"}`;
    const expandBtn = `<button type="button" class="campaign-expand-btn" data-campaign-expand-idx="${i}" data-campaign-name="${encodeURIComponent(r.campaign_name || "")}" aria-expanded="false" aria-label="${escapeHtml(expandLabel)}" title="${escapeHtml(expandLabel)}">▸</button>`;
    return `
    <tr>
      <td>${expandBtn}${escapeHtml(r.campaign_name || "")}${campaignAliasHtml(r.aliases)}${unmapped ? "" : spendProofButton(r.campaign_id, r.campaign_name)}</td>
      <td>${spendCell}</td>
      <td>${fmtCount(r.leads)}</td>
      <td>${fmtCount(r.sqls)}</td>
      <td>${fmtCount(r.customers)}</td>
      <td>${fmtMoney(r.won_revenue)}</td>
      <td>${roasCell}</td>
      <td>${decisionCell}</td>
    </tr>
    <tr class="campaign-drilldown-row" data-drilldown-idx="${i}" hidden>
      <td colspan="8"><div class="campaign-drilldown"></div></td>
    </tr>
  `;
  }).join("");

  // PR-ADS-123: a real horizontal-scroll shell so the Decision column is never
  // clipped on desktop; the table keeps a min-width and scrolls only if needed.
  return `
    <div class="revenue-table-shell">
      <div class="revenue-table-scroll">
        <table class="data-table roas-table revenue-decision-table">
          ${headerRow}
          <tbody>${bodyRows}</tbody>
        </table>
      </div>
    </div>
  `;
}

// Decision-bucket filter chips (campaign page only) — event delegation so the
// chips work after every re-render. Scoped via the data attribute so the
// ROAS by Country page is unaffected.
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-roas-campaign-filter]");
  if (!btn) return;
  roasCampaignFilter = btn.dataset.roasCampaignFilter || "all";
  renderRoasCampaignsPage();
});

// ── PR-ADS-130: ROAS by Campaign row drilldown (clients / deals behind revenue) ──
// Expand control toggles a per-campaign drawer that lazily fetches the closed-won
// client/deal detail from the read-only campaign-detail endpoint. Details are
// rendered exactly as the backend provides them — missing company/contact ids
// show "unavailable", never a fabricated value, and the deal is never hidden.

// Format a possibly-missing value: real value or an explicit "unavailable" chip.
function drilldownValue(v) {
  if (v === null || v === undefined || v === "") {
    return '<span class="detail-unavailable">unavailable</span>';
  }
  return escapeHtml(String(v));
}

function renderCampaignDrilldown(data) {
  const status = (data && data.source_health && data.source_health.status) || "available";
  if (status === "database_unavailable") {
    return `<p class="revenue-footnote">Client/deal detail is temporarily unavailable (durable ledger unreachable).</p>`;
  }
  const details = (data && data.details) || [];
  if (!details.length) {
    return `<p class="revenue-footnote">No attributed closed-won client detail found for this campaign.</p>`;
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
    <div class="campaign-drilldown__title">Clients / Deals Behind This Campaign</div>
    <div class="table-scroll campaign-drilldown__scroll">
      <table class="data-table revenue-decision-table campaign-drilldown-table">
        ${header}
        <tbody>${body}</tbody>
      </table>
    </div>`;
}

async function loadCampaignDrilldown(campaignName, drawerRow) {
  const container = drawerRow.querySelector(".campaign-drilldown");
  if (!container) return;
  container.innerHTML = '<p class="revenue-footnote">Loading clients / deals…</p>';
  try {
    const window_ = getRoasBusinessWindow();
    const res = await fetch(
      `/api/revenue-performance/campaign-detail?window=${encodeURIComponent(window_)}&campaign=${encodeURIComponent(campaignName)}`,
      { credentials: "same-origin" }
    );
    if (!res.ok) {
      container.innerHTML = `<p class="revenue-footnote">Could not load client/deal detail for this campaign.</p>`;
      return;
    }
    container.innerHTML = renderCampaignDrilldown(await res.json());
  } catch (err) {
    console.error("[loadCampaignDrilldown]", err);
    container.innerHTML = `<p class="revenue-footnote">Error loading client/deal detail.</p>`;
  }
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-campaign-expand-idx]");
  if (!btn) return;
  const idx = btn.getAttribute("data-campaign-expand-idx");
  const name = decodeURIComponent(btn.getAttribute("data-campaign-name") || "");
  const drawer = document.querySelector(`.campaign-drilldown-row[data-drilldown-idx="${idx}"]`);
  if (!drawer) return;
  if (drawer.hasAttribute("hidden")) {
    drawer.removeAttribute("hidden");
    btn.setAttribute("aria-expanded", "true");
    btn.textContent = "▾";
    // Lazy-load once; a loaded drawer keeps its content when re-opened.
    if (!drawer.dataset.loaded) {
      drawer.dataset.loaded = "1";
      loadCampaignDrilldown(name, drawer);
    }
  } else {
    drawer.setAttribute("hidden", "");
    btn.setAttribute("aria-expanded", "false");
    btn.textContent = "▸";
  }
});

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

// ── ROAS by Country ───────────────────────────────────────────────────────

let roasCountriesData = [];
let roasCountriesStatus = "idle";

// PR-ADS-112: ROAS by Country is its own decision page. Its readiness needs the
// shared revenue truth layer to be safe AND country-level Google Ads geo spend
// to be available — Campaign ROAS being ready does not imply Country is ready.
let roasCountrySourceHealth = null;

// Frontend-only decision-bucket filter (independent of the Campaign page).
let roasCountryFilter = "all";

function countryNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}

// Country totals are derived from the actual loaded country rows — NOT from the
// global account summary, which may include spend not represented as country
// rows (e.g. unmapped geo). Honesty over a prettier number.
function summarizeRoasCountryRows(rows) {
  const totals = rows.reduce((sum, row) => ({
    spend: sum.spend + countryNumber(row.spend),
    leads: sum.leads + countryNumber(row.leads),
    sqls: sum.sqls + countryNumber(row.sqls),
    customers: sum.customers + countryNumber(row.customers),
    won_revenue: sum.won_revenue + countryNumber(row.won_revenue),
  }), {
    spend: 0, leads: 0, sqls: 0, customers: 0, won_revenue: 0,
  });

  return {
    ...totals,
    roas: totals.spend > 0 ? totals.won_revenue / totals.spend : null,
  };
}

function filterRoasCountryRows(rows, filter) {
  if (!filter || filter === "all") return rows;
  return rows.filter((r) => r.verdict === filter);
}

// Business-value sort: outcomes (customers, revenue) outrank verdict labels.
function sortRoasCountryRows(rows) {
  return [...rows].sort((a, b) =>
    (b.customers || 0) - (a.customers || 0) ||
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

function renderRoasCountrySummary(summary) {
  const s = summary || {};
  return `
    <h3 class="revenue-summary-heading">Country-attributed totals</h3>
    <div class="revenue-summary-strip">
      <div><span>Spend</span><strong>${fmtMoney(s.spend)}</strong></div>
      <div><span>Leads</span><strong>${fmtCount(s.leads)}</strong></div>
      <div><span>SQLs</span><strong>${fmtCount(s.sqls)}</strong></div>
      <div><span>Customers</span><strong>${fmtCount(s.customers)}</strong></div>
      <div><span>Won Revenue</span><strong>${fmtMoney(s.won_revenue)}</strong></div>
      <div><span>ROAS</span><strong>${fmtRoasMultiple(s.roas)}</strong></div>
    </div>
  `;
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
  const leadsReady = sh.lead_date_grain_status === "event_date" && sh.lead_metrics_status !== "withheld";
  const revenueReady = isRevenueIntegrationConnected(sh);
  const chip = (label, ok, okText, warnText) =>
    `<div class="revenue-status-chip revenue-status-chip--${ok ? "ok" : "warning"}">
       <span>${label}</span><strong>${ok ? okText : warnText}</strong>
     </div>`;
  const nextStep = !leadsReady
    ? `<strong>Next step:</strong> Open <strong>System Status → Revenue Health</strong> and run <strong>Lead Event-Date Reconciliation</strong>${!countrySpendReady ? ", then run the Google Ads geo sync" : ""}.`
    : (!countrySpendReady
        ? "<strong>Next step:</strong> Run the Google Ads geo sync, then re-check this page."
        : "<strong>Next step:</strong> Connect revenue attribution via <strong>Revenue Truth Recovery</strong> in <strong>System Status → Revenue Health</strong>.");
  return `
    <div class="revenue-blocked-card">
      <div class="revenue-blocked-card__eyebrow">Revenue attribution</div>
      <h3>Country ROAS is not ready yet</h3>
      <p>This page is blocked because country-level data is not safe enough for business decisions.</p>
      <div class="revenue-status-grid">
        ${chip("Country spend", countrySpendReady, "Available", "Rebuilding")}
        ${chip("Leads / SQLs", leadsReady, "Available", "Rebuilding")}
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
      <p>Country spend and revenue sources are ready, but no HubSpot closed-won deals are attributed to countries in this business window.</p>
    </div>`;
}

// PR-ADS-124: a real recovery CTA from the blocked country card. "Open Revenue
// Health" navigates; the Run actions are admin-only and kick the corresponding
// background job after navigating to Revenue Health. A non-admin sees guidance
// to ask an admin — never a dead-end "run geo sync" with no button. Defined here
// (function declarations hoist) so it never widens the renderRoasCountriesPage →
// table region that other tests scan.
function roasCountryCtaButtons() {
  const isAdmin = _currentUser && _currentUser.role === "admin";
  const runButtons = isAdmin
    ? `<button type="button" class="btn btn--primary" data-country-cta="run-geo-sync">Run Google Ads Geo Sync</button>
       <button type="button" class="btn btn--secondary" data-country-cta="run-spend-backfill">Run Google Ads Spend Backfill</button>`
    : `<p class="revenue-footnote">Ask an admin to run Google Ads Geo Sync from Revenue Health.</p>`;
  return `
    <div class="revenue-cta-actions">
      <button type="button" class="btn btn--secondary" data-country-cta="open-health">Open Revenue Health</button>
      ${runButtons}
    </div>`;
}

async function loadRoasCountries() {
  const window_ = getRoasBusinessWindow();
  const token = ++_revReqSeq.countries;
  setWindowRangeLoading("roas-countries-range");

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
  const kpiGrid = document.getElementById("roas-countries-kpis");
  const tableBody = document.getElementById("roas-countries-table-body");
  if (kpiGrid) kpiGrid.innerHTML = "";
  if (!tableBody) return;

  const data = roasCountryMart;
  if (!data) { tableBody.innerHTML = renderMartUnavailable(); return; }

  // Blocked: the MART says revenue truth is not decision-ready (lead grain /
  // revenue integration). The frontend does not decide this.
  if (!data.readiness || data.readiness.revenue_decision_ready !== true) {
    tableBody.innerHTML = renderCountryRevenueBlockedState(roasCountrySourceHealth);
    return;
  }

  // Country ROAS readiness is the MART's verdict: spend_truth.country_spend_status.
  // Anything other than "verified" (mismatch / unavailable) blocks the trusted
  // table — the frontend never independently decides country ROAS readiness, and
  // never renders a partial/geo/unreconciled denominator as ROAS.
  // PR-ADS-131: "reconciled_with_residual" is a SAFE render state — the real
  // country rows use country-attributed geo spend, and an explicit "Unattributed /
  // No Country" residual bucket carries the shortfall geographic_view could not
  // assign to a country. Only "verified" and "reconciled_with_residual" render the
  // table; every other status stays blocked (mismatch / unavailable / FX or spend
  // incomplete). The frontend never decides this — it obeys the mart's verdict.
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

  tableBody.innerHTML = `
    ${renderMartSpendTruth(data)}
    ${residualMode ? renderCountryResidualWarning(st) : ""}
    ${renderMartSummaryStrip(data)}
    ${renderMartDiagnostics(data)}
    ${renderRoasCountryFilters()}
    ${renderRoasCountryTable(filtered)}
    <p class="revenue-footnote">Canonical Revenue Decision Mart — Google Ads canonical geo spend provides country spend; HubSpot closed-won deals provide revenue.</p>
  `;
}

// PR-ADS-131: compact warning shown above the country table when spend is
// reconciled WITH an unattributed residual. It explains that most spend was
// assigned to countries and the shortfall is shown as "Unattributed / No Country"
// instead of being spread across real countries. Never presents "run geo sync" as
// the fix — the residual is a by-design property of the geographic_view report.
function renderCountryResidualWarning(spendTruth) {
  const st = spendTruth || {};
  const cur = st.native_currency || "GBP";
  const resNative = (st.country_residual_native == null)
    ? "—" : fmtNativeMoney(st.country_residual_native, cur);
  const pct = (st.country_residual_pct == null) ? "" : ` (${st.country_residual_pct}%)`;
  return `
    <div class="revenue-residual-warning">
      <div class="revenue-residual-warning__eyebrow">Country spend includes an unattributed residual</div>
      <p>Google Ads assigned most spend to countries, but ${resNative}${pct} could not be assigned to a country by the geo report.</p>
      <p class="revenue-residual-warning__note">The residual is shown as “Unattributed / No Country” instead of being spread across real countries. Real country rows use country-attributed spend only.</p>
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
function renderCountrySpendUnreconciledState(spendTruth) {
  const st = spendTruth || {};
  const status = st.country_spend_status;
  const isMismatch = status === "mismatch";
  const cause = isMismatch
    ? "Country geo spend exists, but it does not reconcile with canonical Google Ads campaign spend for this window."
    : "There is no canonical Google Ads geo spend for this window yet, so country ROAS cannot be computed safely.";
  const nextStep = isMismatch
    ? "Open Revenue Health and review the Google Ads Geo Sync totals."
    : "Run Google Ads Geo Sync in Revenue Health to populate country spend, then re-check this page.";
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
      <h3>Country ROAS is blocked</h3>
      <p>${cause}</p>
      ${totalsHtml}
      <div class="revenue-status-label">Current status</div>
      <div class="revenue-status-grid">
        ${chip("Campaign spend", labelizeStatus(st.campaign_spend_status), st.campaign_spend_status === "verified")}
        ${chip("FX coverage", labelizeStatus(st.fx_status), st.fx_status === "verified")}
        ${chip("Country geo spend", labelizeStatus(status), false)}
      </div>
      ${rootCauseHtml}
      <div class="revenue-next-step"><strong>Next step:</strong> ${escapeHtml(effectiveNextStep)}</div>
      ${roasCountryCtaButtons()}
    </div>`;
}

// PR-ADS-132: expand control for a REAL country row — opens the per-country
// clients/deals drawer (lazy-loaded). Carries the country name + ISO code so the
// lazy loader can query the safer code identity. Residual rows never call this.
function countryExpandControl(r, i) {
  const label = `Show clients / deals behind ${countryDisplayName(r.country) || "this country"}`;
  return `<button type="button" class="country-expand-button" data-country-expand data-country-idx="${i}" data-country-name="${encodeURIComponent(r.country || "")}" data-country-code="${encodeURIComponent(r.country_code || "")}" aria-expanded="false" aria-label="${escapeHtml(label)}" title="${escapeHtml(label)}">▸</button>`;
}

function renderRoasCountryTable(rows) {
  if (!rows.length) {
    return `
      <div class="revenue-empty-inline">
        No countries match this decision filter.
      </div>
    `;
  }

  const headerRow = `
    <tr>
      <th>Country</th>
      <th>Spend</th>
      <th>Leads</th>
      <th>SQLs</th>
      <th>Customers</th>
      <th>Won Revenue</th>
      <th>ROAS</th>
      <th>Decision</th>
    </tr>
  `;

  // PR-ADS-131: the residual row is NOT a real country — muted styling, a
  // "Residual" badge, no flag, an "Unattributed" decision, and em-dashes for the
  // metric columns (never fake zeros/ROAS). Only its spend is shown. PR-ADS-132:
  // real country rows get an expand control (see countryExpandControl); the
  // residual row never does.
  const bodyRows = rows.map((r, i) => {
    if (r && r.is_residual) {
      return `
    <tr class="revenue-residual-row">
      <td><span class="revenue-residual-name">Unattributed / No Country</span> <span class="revenue-residual-badge">Residual</span></td>
      <td>${fmtMoney(r.spend)}</td>
      <td>—</td>
      <td>—</td>
      <td>—</td>
      <td>—</td>
      <td>—</td>
      <td><span class="decision-badge decision-badge--unattributed">Unattributed</span></td>
    </tr>`;
    }
    return `
    <tr>
      <td>${countryExpandControl(r, i)}${escapeHtml(countryDisplayName(r.country))}</td>
      <td>${fmtMoney(r.spend)}</td>
      <td>${fmtCount(r.leads)}</td>
      <td>${fmtCount(r.sqls)}</td>
      <td>${fmtCount(r.customers)}</td>
      <td>${fmtMoney(r.won_revenue)}</td>
      <td>${fmtRoasMultiple(r.roas)}</td>
      <td>${getBusinessVerdictBadge(r.verdict)}</td>
    </tr>
    <tr class="country-drilldown-row" data-country-drilldown-idx="${i}" hidden>
      <td colspan="8"><div class="country-drilldown-panel"></div></td>
    </tr>`;
  }).join("");

  // PR-ADS-123: same contained horizontal-scroll shell as ROAS by Campaign so
  // the Decision column stays fully visible on desktop.
  return `
    <div class="revenue-table-shell">
      <div class="revenue-table-scroll">
        <table class="data-table roas-table revenue-decision-table">
          ${headerRow}
          <tbody>${bodyRows}</tbody>
        </table>
      </div>
    </div>
  `;
}

// ── PR-ADS-132: ROAS by Country row drilldown (clients / deals behind revenue) ──
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

// Stable key: window + country identity (prefer the safer ISO code).
function countryDrilldownKey(country, countryCode) {
  const id = (countryCode || country || "").toString();
  return `${getRoasBusinessWindow()}:${id}`;
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

async function loadCountryDrilldown(country, countryCode, drawerRow) {
  const container = drawerRow.querySelector(".country-drilldown-panel");
  if (!container) return;
  const key = countryDrilldownKey(country, countryCode);
  if (roasCountryDrilldownCache[key]) {
    container.innerHTML = renderCountryDrilldown(roasCountryDrilldownCache[key]);
    return;
  }
  container.innerHTML = `<p class="country-drilldown-muted">Loading country client details…</p>`;
  roasCountryDrilldownLoading[key] = true;
  try {
    const window_ = getRoasBusinessWindow();
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

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-country-expand]");
  if (!btn) return;
  const idx = btn.getAttribute("data-country-idx");
  const country = decodeURIComponent(btn.getAttribute("data-country-name") || "");
  const code = decodeURIComponent(btn.getAttribute("data-country-code") || "");
  const drawer = document.querySelector(`.country-drilldown-row[data-country-drilldown-idx="${idx}"]`);
  if (!drawer) return;
  if (drawer.hasAttribute("hidden")) {
    drawer.removeAttribute("hidden");
    btn.setAttribute("aria-expanded", "true");
    btn.textContent = "▾";
    roasCountryExpandedKey = countryDrilldownKey(country, code);
    // Lazy-load once; a loaded drawer keeps its content when re-opened.
    if (!drawer.dataset.loaded) {
      drawer.dataset.loaded = "1";
      loadCountryDrilldown(country, code, drawer);
    }
  } else {
    drawer.setAttribute("hidden", "");
    btn.setAttribute("aria-expanded", "false");
    btn.textContent = "▸";
  }
});

// Decision-bucket filter chips (country page only) — event delegation keyed off
// the country-specific data attribute so the Campaign page is unaffected.
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-roas-country-filter]");
  if (!btn) return;
  roasCountryFilter = btn.dataset.roasCountryFilter || "all";
  renderRoasCountriesPage();
});

// PR-ADS-124: blocked ROAS by Country card CTAs. "Open Revenue Health" navigates;
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

// ── Unit Economics ─────────────────────────────────────────────────────────

let unitEconomicsData = null;
let unitEconomicsStatus = "idle";

async function loadUnitEconomics() {
  const window_ = getCurrentWindowParam();
  if (!VALID_ROAS_WINDOWS.includes(window_)) return;

  unitEconomicsStatus = "loading";
  const body = document.getElementById("unit-economics-table-body");
  if (body) body.innerHTML = '<p class="empty-state" style="padding:var(--space-5)">Loading unit economics…</p>';

  try {
    const res = await fetch(`/api/reports/unit-economics?window=${window_}`, { credentials: "same-origin" });
    if (!res.ok) {
      unitEconomicsStatus = "error";
      if (body) body.innerHTML = '<p class="empty-state" style="padding:var(--space-5)">Failed to load unit economics.</p>';
      return;
    }
    unitEconomicsData = await res.json();
    unitEconomicsStatus = "ok";
    renderUnitEconomicsPage();
  } catch (err) {
    console.error("[loadUnitEconomics]", err);
    unitEconomicsStatus = "error";
    if (body) body.innerHTML = '<p class="empty-state" style="padding:var(--space-5)">Error loading unit economics.</p>';
  }
}

function renderUnitEconomicsPage() {
  const kpiGrid = document.getElementById("unit-economics-kpis");
  const verdictEl = document.getElementById("unit-economics-verdict");
  const tableBody = document.getElementById("unit-economics-table-body");

  if (!unitEconomicsData) {
    if (tableBody) tableBody.innerHTML = '<p class="empty-state" style="padding:var(--space-5)">No unit economics data available.</p>';
    renderPageExplanation("unit-economics", { forceVisible: true });
    return;
  }

  const d = unitEconomicsData;
  const overall = d.overall || d;

  if (kpiGrid) {
    kpiGrid.innerHTML = `
      <div class="kpi-card"><div class="kpi-card__label">LTV/CAC</div><div class="kpi-card__value">${fmtNum(overall.ltv_to_cac || overall.ltv_cac)}x</div></div>
      <div class="kpi-card"><div class="kpi-card__label">Payback Months</div><div class="kpi-card__value">${fmtNum(overall.payback_months)}</div></div>
      <div class="kpi-card"><div class="kpi-card__label">Avg Deal ACV</div><div class="kpi-card__value">${fmtMoney(overall.avg_deal_acv || overall.average_deal_acv)}</div></div>
      <div class="kpi-card"><div class="kpi-card__label">Avg Deal MRR</div><div class="kpi-card__value">${fmtMoney(overall.avg_deal_mrr || overall.average_deal_mrr)}</div></div>
      <div class="kpi-card"><div class="kpi-card__label">Monthly Churn Rate</div><div class="kpi-card__value">${fmtNum((overall.monthly_churn_rate_used ?? 0) * 100, 1)}%</div></div>
    `;
  }

  if (verdictEl) {
    const v = overall.verdict || overall.overall_verdict || "—";
    const explanations = {
      SCALE: "Strong unit economics — customer acquisition is profitable at scale.",
      HOLD: "Watch closely — not enough scale confidence yet.",
      FIX: "Economics are weak or payback is too long.",
      CUT: "Losing money on customer acquisition.",
      INSUFFICIENT_DATA: "Not enough closed-won volume to produce a reliable verdict."
    };
    verdictEl.innerHTML = `
      <div class="unit-economics-verdict-card">
        <span class="${getVerdictBadgeClass(v)}">${escapeHtml(v)}</span>
        <span class="unit-economics-verdict-explanation">${escapeHtml(explanations[v] || "")}</span>
      </div>
    `;
  }

  const campaigns = d.by_campaign || d.campaigns || [];
  if (!campaigns.length) {
    if (tableBody) tableBody.innerHTML = '<p class="empty-state" style="padding:var(--space-5)">No campaign-level economics data.</p>';
    return;
  }

  if (tableBody) {
    const headerRow = `<tr>
      <th>Campaign</th><th>Spend</th><th>Deals Won</th><th>CAC</th><th>LTV/CAC</th>
      <th>Payback Mo.</th><th>Verdict</th>
    </tr>`;
    const rows = campaigns.map((r) => `<tr>
      <td>${escapeHtml(r.campaign || "")}</td>
      <td>${fmtMoney(r.spend)}</td>
      <td>${fmt(r.deals_won)}</td>
      <td>${fmtMoney(r.cac)}</td>
      <td>${fmtNum(r.ltv_to_cac || r.ltv_cac)}x</td>
      <td>${fmtNum(r.payback_months)}</td>
      <td><span class="${getVerdictBadgeClass(r.verdict)}">${escapeHtml(r.verdict || "")}</span></td>
    </tr>`).join("");
    tableBody.innerHTML = `<div class="table-scroll"><table class="data-table roas-table">${headerRow}<tbody>${rows}</tbody></table></div>`;
  }
}

// ── Churn Input ───────────────────────────────────────────────────────────

let churnInputData = null;

async function loadChurnInput() {
  const currentEl = document.getElementById("churn-input-current");
  if (currentEl) currentEl.innerHTML = '<p class="empty-state">Loading churn configuration…</p>';

  try {
    const res = await fetch("/api/admin/churn-input", { credentials: "same-origin" });
    if (!res.ok) {
      if (currentEl) currentEl.innerHTML = '<p class="empty-state">Failed to load churn configuration. Admin access required.</p>';
      return;
    }
    churnInputData = await res.json();
    renderChurnInputPage();
  } catch (err) {
    console.error("[loadChurnInput]", err);
    if (currentEl) currentEl.innerHTML = '<p class="empty-state">Error loading churn configuration.</p>';
  }
}

function renderChurnInputPage() {
  const currentEl = document.getElementById("churn-input-current");
  if (!currentEl || !churnInputData) return;

  const d = churnInputData;
  const defaultChurn = d.default_monthly_churn || 0.03;
  const monthlyOverrides = d.monthly || {};
  const campaignOverrides = d.campaign_overrides || {};

  let html = '<div class="churn-input-summary">';
  html += `<div class="churn-input-section"><h3>Default Monthly Churn</h3><p class="churn-input-value">${(defaultChurn * 100).toFixed(1)}%</p></div>`;

  html += '<div class="churn-input-section"><h3>Monthly Overrides</h3>';
  const monthEntries = Object.entries(monthlyOverrides);
  if (monthEntries.length) {
    html += '<table class="data-table churn-table"><tr><th>Month</th><th>Rate</th></tr>';
    monthEntries.sort((a, b) => b[0].localeCompare(a[0])).forEach(([month, rate]) => {
      html += `<tr><td>${escapeHtml(month)}</td><td>${(rate * 100).toFixed(1)}%</td></tr>`;
    });
    html += '</table>';
  } else {
    html += '<p class="empty-state">No monthly overrides configured.</p>';
  }
  html += '</div>';

  html += '<div class="churn-input-section"><h3>Campaign Overrides</h3>';
  const campEntries = Object.entries(campaignOverrides);
  if (campEntries.length) {
    html += '<table class="data-table churn-table"><tr><th>Campaign</th><th>Rate</th></tr>';
    campEntries.forEach(([campaign, rate]) => {
      html += `<tr><td>${escapeHtml(campaign)}</td><td>${(rate * 100).toFixed(1)}%</td></tr>`;
    });
    html += '</table>';
  } else {
    html += '<p class="empty-state">No campaign overrides configured.</p>';
  }
  html += '</div></div>';

  currentEl.innerHTML = html;
}

async function handleChurnInputSubmit(e) {
  e.preventDefault();
  const errorEl = document.getElementById("churn-input-error");
  const successEl = document.getElementById("churn-input-success");
  if (errorEl) { errorEl.hidden = true; errorEl.textContent = ""; }
  if (successEl) { successEl.hidden = true; successEl.textContent = ""; }

  const monthInput = document.getElementById("churn-month");
  const rateInput = document.getElementById("churn-rate");
  const month = (monthInput ? monthInput.value : "").trim();
  const rate = rateInput ? parseFloat(rateInput.value) : NaN;

  // Validate month format YYYY-MM
  if (!/^\d{4}-\d{2}$/.test(month)) {
    if (errorEl) { errorEl.textContent = "Month must be in YYYY-MM format (e.g. 2026-05)."; errorEl.hidden = false; }
    return;
  }
  // Validate rate between 0 and 1
  if (isNaN(rate) || rate < 0 || rate > 1) {
    if (errorEl) { errorEl.textContent = "Churn rate must be a number between 0 and 1 (e.g. 0.03 for 3%)."; errorEl.hidden = false; }
    return;
  }

  try {
    const res = await fetch("/api/admin/churn-input", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ month, rate }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      const msg = body.detail || body.error || `Server returned ${res.status}`;
      if (errorEl) { errorEl.textContent = msg; errorEl.hidden = false; }
      return;
    }
    if (successEl) { successEl.textContent = `Churn override saved: ${month} → ${(rate * 100).toFixed(1)}%`; successEl.hidden = false; }
    // Reload config to show updated values
    loadChurnInput();
  } catch (err) {
    console.error("[handleChurnInputSubmit]", err);
    if (errorEl) { errorEl.textContent = "Network error. Please try again."; errorEl.hidden = false; }
  }
}

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

// ── Revenue Snapshot History (PR-ADS-083A) ────────────────────────────────

let snapshotHistoryStatus = "idle"; // idle | loading | ok | empty | error

async function loadRevenueSnapshotHistory() {
  const container = document.getElementById("snapshot-history-container");
  if (!container) return;

  snapshotHistoryStatus = "loading";
  container.innerHTML = '<p class="empty-state" style="padding:var(--space-5)">Loading revenue snapshots…</p>';

  try {
    const [latestResult, listResult] = await Promise.allSettled([
      fetchJSON(`/api/reports/roas/snapshots/latest?window=${getCurrentWindowParam()}`),
      fetchJSON(`/api/reports/roas/snapshots?window=${getCurrentWindowParam()}&limit=30`),
    ]);

    let latestData = null;
    let listData = null;

    if (latestResult.status === "fulfilled") {
      latestData = latestResult.value;
    } else if (!_isNotFoundError(latestResult.reason)) {
      throw latestResult.reason;
    }
    if (listResult.status === "fulfilled") {
      listData = listResult.value;
    } else if (!_isNotFoundError(listResult.reason)) {
      throw listResult.reason;
    }

    const snapshots = (listData && listData.snapshots) || [];

    if (!latestData && snapshots.length === 0) {
      snapshotHistoryStatus = "empty";
      container.innerHTML = `
        <div class="empty-state-block empty-state--info">
          <p class="empty-state__title">No revenue snapshots exist yet.</p>
          <p class="empty-state__body"><strong>Next:</strong> Snapshots are generated daily by the ROAS snapshot scheduler. Check Data Runs for scheduler status.</p>
        </div>`;
      return;
    }

    snapshotHistoryStatus = "ok";
    let html = "";

    // Latest snapshot card
    if (latestData) {
      const summary = latestData.summary || {};
      const ue = latestData.unit_economics || {};
      html += `
        <div class="snapshot-latest-card">
          <h3 class="snapshot-latest-card__title">Latest Snapshot</h3>
          <div class="snapshot-latest-card__meta">
            <span class="context-chip context-chip--source">Date: ${escapeHtml(latestData.snapshot_date || "—")}</span>
            <span class="context-chip context-chip--depends">Window: ${escapeHtml(latestData.window || "60d")}</span>
            <span class="context-chip context-chip--readonly">Generated: ${fmtDate(latestData.generated_at)}</span>
          </div>
          <div class="kpi-grid snapshot-kpi-grid">
            <div class="kpi-card"><div class="kpi-card__label">Total Spend</div><div class="kpi-card__value">${fmtDollar(summary.total_spend)}</div></div>
            <div class="kpi-card"><div class="kpi-card__label">ACV Revenue</div><div class="kpi-card__value">${fmtDollar(summary.total_acv_revenue)}</div></div>
            <div class="kpi-card"><div class="kpi-card__label">LTV Revenue</div><div class="kpi-card__value">${fmtDollar(summary.total_ltv_revenue)}</div></div>
          </div>
          <div class="verdict-count-grid snapshot-verdict-grid">
            <div class="verdict-count-card verdict-count-card--scale"><div class="verdict-count-card__num">${fmt(summary.scale_count)}</div><div class="verdict-count-card__label">SCALE</div></div>
            <div class="verdict-count-card verdict-count-card--hold"><div class="verdict-count-card__num">${fmt(summary.hold_count)}</div><div class="verdict-count-card__label">HOLD</div></div>
            <div class="verdict-count-card verdict-count-card--fix"><div class="verdict-count-card__num">${fmt(summary.fix_count)}</div><div class="verdict-count-card__label">FIX</div></div>
            <div class="verdict-count-card verdict-count-card--cut"><div class="verdict-count-card__num">${fmt(summary.cut_count)}</div><div class="verdict-count-card__label">CUT</div></div>
            <div class="verdict-count-card"><div class="verdict-count-card__num">${fmt(summary.insufficient_data_count)}</div><div class="verdict-count-card__label">INSUFFICIENT</div></div>
          </div>`;

      // LTV/CAC and payback if available
      if (ue.ltv_cac_ratio !== undefined && ue.ltv_cac_ratio !== null) {
        html += `<div class="snapshot-economics"><span><strong>LTV/CAC:</strong> ${Number(ue.ltv_cac_ratio).toFixed(2)}x</span>`;
        if (ue.payback_months !== undefined && ue.payback_months !== null) {
          html += ` <span><strong>Payback:</strong> ${Number(ue.payback_months).toFixed(1)} months</span>`;
        }
        html += `</div>`;
      }

      // Warnings
      const warnings = latestData.warnings || [];
      if (warnings.length > 0) {
        html += `<div class="snapshot-warnings"><strong>⚠ Warnings:</strong><ul>${warnings.map(w => `<li>${escapeHtml(w)}</li>`).join("")}</ul></div>`;
      }

      html += `</div>`; // close snapshot-latest-card
    }

    // History table
    if (snapshots.length > 0) {
      html += `
        <div class="panel" style="margin-top:var(--space-4)">
          <div class="panel__header">Snapshot History (last ${snapshots.length} runs)</div>
          <div class="panel__body panel__body--flush">
            <div class="table-scroll">
              <table class="data-table">
                <thead><tr>
                  <th>Date</th>
                  <th>Window</th>
                  <th>Spend</th>
                  <th>ACV Revenue</th>
                  <th>LTV Revenue</th>
                  <th>SCALE</th>
                  <th>HOLD</th>
                  <th>FIX</th>
                  <th>CUT</th>
                </tr></thead>
                <tbody>`;
      for (const snap of snapshots) {
        const s = snap.summary || {};
        html += `<tr>
          <td>${escapeHtml(snap.snapshot_date || "—")}</td>
          <td>${escapeHtml(snap.window || "—")}</td>
          <td>${fmtDollar(s.total_spend)}</td>
          <td>${fmtDollar(s.total_acv_revenue)}</td>
          <td>${fmtDollar(s.total_ltv_revenue)}</td>
          <td>${fmt(s.scale_count)}</td>
          <td>${fmt(s.hold_count)}</td>
          <td>${fmt(s.fix_count)}</td>
          <td>${fmt(s.cut_count)}</td>
        </tr>`;
      }
      html += `</tbody></table></div></div></div>`;
    }

    container.innerHTML = html;
  } catch (err) {
    console.error("[loadRevenueSnapshotHistory]", err);
    snapshotHistoryStatus = "error";
    container.innerHTML = '<p class="empty-state" style="padding:var(--space-5)">Failed to load revenue snapshots.</p>';
  }
}

function _isNotFoundError(err) {
  if (err?.status === 404) return true;
  const msg = String(err?.message ?? err ?? "");
  return msg === "HTTP 404" || /not found/i.test(msg);
}
