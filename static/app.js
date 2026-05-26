/**
 * static/app.js
 *
 * Logistaas Ads Intelligence — 5-page SPA frontend logic.
 * PR-ADS-025B — Dashboard Live Data Upgrade
 *
 * Rules:
 *  - No hardcoded secrets.
 *  - No external tracking or analytics.
 *  - No third-party JS dependencies.
 *  - Auth state managed via HTTP-only session cookie (not accessible to JS).
 *  - Role-based UI: admin sees run triggers + health page; viewer/mdr read-only.
 *  - SPA routing via show/hide — no window.location changes, no hash routing.
 */

"use strict";

// ── Constants ──────────────────────────────────────────────────────────────

const PAGES = ["dashboard", "reports", "campaigns", "waste", "search-terms", "ngrams", "geo", "keywords", "leads", "deals", "gclid-attribution", "opportunities", "scheduler", "health", "action-queue", "backfill", "historical-intelligence"];

// Deal pipeline stages (Phase 1 read-only reference)
const DEAL_PIPELINE_STAGES = ["Proposal", "Trials", "Pricing Acceptance", "Invoice Sent", "Won"];

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
// Derived pages (ngrams, waste) share their source dataset (windsor/search_terms)
// because no separate freshness record exists for the derived output.
const PAGE_DATASET_MAP = {
  campaigns:         ["windsor/campaigns"],
  waste:             ["windsor/search_terms"],   // waste_terms derived from search_terms
  search_terms:      ["windsor/search_terms"],
  ngrams:            ["windsor/search_terms"],   // n-grams derived from search_terms
  geo:               ["windsor/geo"],
  keywords:          ["windsor/keywords"],
  lead_quality:      ["hubspot/contacts"],
  deals:             ["hubspot/deals"],
  gclid_attribution: ["gclid/matches"],
  in_progress_leads: ["hubspot/contacts"],
  action_queue:      ["windsor/campaigns", "hubspot/contacts", "hubspot/deals"],
  reports:           ["windsor/campaigns", "hubspot/contacts", "hubspot/deals", "windsor/search_terms"],
};

// Pages whose output is derived/computed from the source datasets above rather
// than being a direct read of the synced table.  Strip copy uses "Derived from"
// wording for these rather than "Dataset freshness:".
const DERIVED_DATASET_PAGES = new Set(["ngrams", "waste"]);

// ── Session state ──────────────────────────────────────────────────────────

let _currentUser   = null;  // { username, role } or null
let _currentPage   = null;  // active page id string
let _selectedDays  = (() => {
  try {
    const stored = sessionStorage.getItem("ads_days");
    const n = stored ? parseInt(stored, 10) : 30;
    return [7, 14, 30, 60].includes(n) ? n : 30;
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

// Search Terms page state
let searchTermRows = [];
let searchTermsNextCursor = null;
let searchTermsHasMore = false;
let searchTermsStatus = "idle"; // idle | loading | ok | empty | db_unavailable | cursor_error | error

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
// renderPageDatasetFreshness().  Keyed by "source/dataset" (e.g. "windsor/campaigns").
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

function setSelectedDays(days) {
  _selectedDays = days;
  try { sessionStorage.setItem("ads_days", String(days)); } catch (_) { /* ignore */ }
  // Update active button state
  document.querySelectorAll(".time-range-btn").forEach((btn) => {
    btn.classList.toggle("active", parseInt(btn.dataset.days, 10) === days);
  });
  // Close campaign drawer when selected reporting window changes to avoid stale detail.
  closeCampaignDrawer();
  // Reload current page with new window
  if (_currentPage) loadPage(_currentPage);
  loadDataFreshness();
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
  // Start with sidebar health check and data freshness
  loadSidebarHealth();
  loadDataFreshness();
  loadMonitoringStatus();
  // Pre-fetch dataset freshness cache so per-page strips are ready before the
  // user navigates to any data page.  loadDatasetFreshness() guards against
  // missing DOM elements so it is safe to call outside the Health page.
  loadDatasetFreshness();
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
      navigate("dashboard");
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

// ── Router ─────────────────────────────────────────────────────────────────

function navigate(page) {
  // Role enforcement: health page is admin-only
  if (page === "health" && (!_currentUser || _currentUser.role !== "admin")) {
    navigate("dashboard");
    return;
  }
  // Role enforcement: backfill page is admin-only
  if (page === "backfill" && (!_currentUser || _currentUser.role !== "admin")) {
    navigate("dashboard");
    return;
  }

  // Hide all pages
  PAGES.forEach((p) => {
    const el = document.getElementById(`page-${p}`);
    if (el) el.hidden = true;
  });

  // Show target page
  const target = document.getElementById(`page-${page}`);
  if (target) target.hidden = false;

  // Update active nav item
  document.querySelectorAll(".nav-item").forEach((el) => {
    el.classList.toggle("active", el.dataset.page === page);
  });

  _currentPage = page;
  loadPage(page);
}

function loadPage(page) {
  switch (page) {
    case "dashboard":     loadDashboard();                          break;
    case "reports":       loadReports();                            break;
    case "campaigns":     loadCampaigns();                          break;
    case "waste":         loadWaste();                              break;
    case "search-terms":  loadSearchTerms({ reset: true });         break;
    case "ngrams":
      if (ngramsPrefill) {
        const ngramCampaignInput = document.getElementById("ngrams-campaign");
        if (ngramCampaignInput) ngramCampaignInput.value = ngramsPrefill.campaign || "";
        ngramsPrefill = null;
      }
      loadNgrams();
      break;
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
      break;
    case "opportunities": loadOpportunities(); break;
    case "scheduler":     loadScheduler();     break;
    case "health":        loadHealth();        break;
    case "action-queue":  loadActionQueue();   break;
    case "backfill":      loadBackfillStatus(); break;
    case "historical-intelligence": loadHistoricalIntelligence(); break;
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

  let data = null;
  try {
    data = await fetchJSON("/api/monitoring/status");
  } catch (_) {
    // Monitoring endpoint unavailable — fail silently; do not block the UI.
    return;
  }

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
  const names = { windsor: "Windsor", hubspot: "HubSpot", gclid: "GCLID" };
  return names[source] || escapeHtml(source);
}

function _datasetDisplayName(key) {
  const labels = {
    "windsor/campaigns":    "Campaign data",
    "windsor/search_terms": "Search-term data",
    "windsor/keywords":     "Keyword data",
    "windsor/geo":          "Geo data",
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
      fresh_with_data:    "Fresh with data",
      fresh_but_empty:    "Fresh but empty",
      stale_with_data:    "Stale with data",
      stale_and_empty:    "Stale and empty",
      failed:             "Failed",
      running:            "Running",
      not_run:            "Not run",
      dependency_blocked: "Blocked by dependency",
      db_unavailable:     "DB unavailable",
    };
    const _csClasses = {
      fresh_with_data:    "run-meta is-fresh",
      fresh_but_empty:    "run-meta is-canonical-warning",
      stale_with_data:    "run-meta is-stale",
      stale_and_empty:    "run-meta is-stale",
      failed:             "run-meta is-stale",
      running:            "run-meta",
      not_run:            "run-meta",
      dependency_blocked: "run-meta is-canonical-warning",
      db_unavailable:     "run-meta is-stale",
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
  const warningCount = statuses.filter((s) => s === "fresh_but_empty" || s === "stale_with_data" || s === "dependency_blocked").length;
  const errorCount   = statuses.filter((s) => s === "failed" || s === "stale_and_empty" || s === "db_unavailable").length;
  const runningCount = statuses.filter((s) => s === "running").length;
  const unknownCount = statuses.filter((s) => s === "unknown" || s === "not_run").length;
  const totalCount   = datasetKeys.length;

  const detail = datasetKeys.map((key, i) => {
    const _shortLabels = {
      fresh_with_data: "Fresh", fresh_but_empty: "Empty", stale_with_data: "Stale",
      stale_and_empty: "Stale+Empty", failed: "Failed", running: "Running",
      not_run: "Not run", dependency_blocked: "Blocked", db_unavailable: "DB down",
      unknown: "Unknown", success: "Fresh", stale: "Stale",
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

// ── Dashboard page ─────────────────────────────────────────────────────────

async function loadDashboard() {
  const days = getSelectedDays();

  let summary = null;
  try {
    summary = await fetchJSON(`/api/summary?days=${days}`);
  } catch (_) { /* summary unavailable — KPIs show dashes */ }

  renderKPIs(summary);

  // Load run history timeline (non-blocking — failure does not affect other panels)
  loadRunHistory();

  // Load campaign data for the verdict summary panel
  let campaigns = [];
  try {
    const campData = await fetchJSON(`/api/campaigns?days=${days}`);
    campaigns = campData.campaigns || [];
    renderVerdictSummary(campaigns);
  } catch (_) {
    renderVerdictSummaryEmpty();
  }

  // Load dashboard trends (non-blocking — failure does not break dashboard).
  // Alerts panel is upgraded with trend alerts when available; falls back to
  // campaign-verdict alerts otherwise.
  loadDashboardTrends(campaigns);
}

function renderKPIs(summary) {
  const spendEl = document.getElementById("kpi-spend");
  const sqlsEl  = document.getElementById("kpi-sqls");
  const cpqlEl  = document.getElementById("kpi-cpql");
  const wasteEl = document.getElementById("kpi-waste");

  if (!summary) {
    ["kpi-spend", "kpi-sqls", "kpi-cpql", "kpi-waste"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.textContent = "—";
    });
    return;
  }

  if (spendEl) spendEl.textContent = summary.total_spend_usd != null
    ? fmtDollar(summary.total_spend_usd) : "—";
  if (sqlsEl)  sqlsEl.textContent  = summary.confirmed_sqls != null
    ? String(summary.confirmed_sqls) : "0";
  if (cpqlEl)  cpqlEl.textContent  = summary.avg_cpql_usd != null
    ? fmtDollar(summary.avg_cpql_usd) : "N/A";
  if (wasteEl) wasteEl.textContent = summary.confirmed_waste_usd != null
    ? fmtDollar(summary.confirmed_waste_usd) : "—";
}

// campaigns: array of { campaign_name, latest_verdict, avg_spend_usd, ... }
function renderVerdictSummary(campaigns) {
  const el = document.getElementById("dash-verdict-body");
  if (!el) return;

  const real = campaigns.filter((c) => c.avg_spend_usd != null && c.avg_spend_usd > 0);

  if (real.length === 0) {
    el.innerHTML = `<p class="empty-state">No campaign data yet. Trigger a weekly run to populate.</p>`;
    return;
  }

  const sorted   = [...real].sort((a, b) => (b.avg_spend_usd || 0) - (a.avg_spend_usd || 0));
  const maxSpend = sorted[0].avg_spend_usd || 1;

  el.innerHTML = sorted.map((c) => {
    const pct   = Math.max(5, Math.round((c.avg_spend_usd / maxSpend) * 100));
    const v     = (c.latest_verdict || "").toUpperCase();
    const spend = c.avg_spend_usd != null ? fmtDollar(c.avg_spend_usd) : "—";
    return `
      <div class="verdict-row">
        <div class="verdict-row__name" title="${escapeHtml(c.campaign_name)}">${escapeHtml(c.campaign_name)}</div>
        <div class="verdict-row__bar">
          <div class="verdict-row__bar-fill verdict-row__bar-fill--${escapeHtml(v)}" style="width:${pct}%"></div>
        </div>
        <div class="verdict-row__meta">
          <span class="verdict-row__spend">${spend}</span>
          ${verdictBadge(v)}
        </div>
      </div>`;
  }).join("");
}

function renderVerdictSummaryEmpty() {
  const el = document.getElementById("dash-verdict-body");
  if (el) el.innerHTML = `<p class="empty-state">No campaign data yet. Trigger a weekly run to populate.</p>`;
}

function renderAlerts(campaigns) {
  const el = document.getElementById("dash-alerts-body");
  if (!el) return;

  const alerts = (campaigns || []).filter((c) =>
    c.latest_verdict === "FIX" || c.latest_verdict === "CUT"
  );

  if (alerts.length === 0) {
    el.innerHTML = `<p class="empty-state">No active alerts.</p>`;
    return;
  }

  const icon = (v) => v === "CUT"
    ? `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" class="alert-icon alert-icon--cut"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`
    : `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" class="alert-icon alert-icon--fix"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`;

  el.innerHTML = alerts.map((c) => `
    <div class="alert-item">
      ${icon(c.latest_verdict)}
      <div class="alert-text">
        Campaign <span class="alert-campaign">${escapeHtml(c.campaign_name)}</span>
        — verdict ${verdictBadge(c.latest_verdict)}
        ${c.avg_spend_usd != null ? `· Spend: ${fmtDollar(c.avg_spend_usd)}` : ""}
      </div>
    </div>`).join("");
}

function renderAlertsEmpty() {
  const el = document.getElementById("dash-alerts-body");
  if (el) el.innerHTML = `<p class="empty-state">No alerts. Trigger a run to check for issues.</p>`;
}

function renderAlertsUnavailable() {
  const el = document.getElementById("dash-alerts-body");
  if (el) el.innerHTML = `<p class="empty-state">Alerts temporarily unavailable.</p>`;
}

// ── Dashboard trends (What Changed + Campaign Movement + Alerts upgrade) ────

async function loadDashboardTrends(fallbackCampaigns) {
  const trendsEl    = document.getElementById("dashboard-trends");
  const movementEl  = document.getElementById("campaign-movement-list");

  // Show loading states
  if (trendsEl)   trendsEl.innerHTML   = `<p class="empty-state">Loading…</p>`;
  if (movementEl) movementEl.innerHTML = `<p class="empty-state" style="padding:var(--space-5)">Loading…</p>`;

  // Whether /api/campaigns returned actual data — used for fallback alert distinction
  const hasFallbackData = Array.isArray(fallbackCampaigns) && fallbackCampaigns.length > 0;

  let data = null;
  try {
    data = await fetchJSON(`/api/dashboard/trends?days=${getSelectedDays()}`);
  } catch (_) {
    // Endpoint unavailable — fall back to campaign-verdict alerts
    if (trendsEl)   trendsEl.innerHTML   = `<p class="empty-state">Trend data temporarily unavailable.</p>`;
    if (movementEl) movementEl.innerHTML = `<p class="empty-state" style="padding:var(--space-5)">Campaign movement data unavailable.</p>`;
    if (hasFallbackData) {
      renderAlerts(fallbackCampaigns);
    } else {
      renderAlertsUnavailable();
    }
    return;
  }

  if (data.db_unavailable) {
    if (trendsEl)   trendsEl.innerHTML   = `<p class="empty-state">Trend data temporarily unavailable — database offline.</p>`;
    if (movementEl) movementEl.innerHTML = `<p class="empty-state" style="padding:var(--space-5)">Campaign movement temporarily unavailable — database offline.</p>`;
    if (hasFallbackData) {
      renderAlerts(fallbackCampaigns);
    } else {
      renderAlertsUnavailable();
    }
    return;
  }

  const dq = data.data_quality || {};
  const hasPrevious = dq.has_previous_period === true;

  // ── Render summary trend cards ─────────────────────────────────────────────
  renderTrendSummary(data.summary || {}, hasPrevious, trendsEl);

  // ── Render campaign movement list ──────────────────────────────────────────
  renderCampaignMovements(data.campaign_movements || [], hasPrevious, movementEl);

  // ── Upgrade alerts panel with trend alerts (fall back to campaign verdicts) ─
  const trendAlerts = data.alerts || [];
  if (trendAlerts.length > 0) {
    renderTrendAlerts(trendAlerts);
  } else if (hasFallbackData) {
    renderAlerts(fallbackCampaigns);
  } else {
    renderAlertsUnavailable();
  }
}

// Direction semantics:
//   spend   — up is neutral (context-dependent), down is neutral
//   sqls    — up is good,  down is bad
//   waste   — up is bad,   down is good
//   junkRate — up is bad,  down is good
const TREND_DIRECTION = {
  spend_usd:           { up: "neutral", down: "neutral" },
  confirmed_sqls:      { up: "good",    down: "bad"     },
  confirmed_waste_usd: { up: "bad",     down: "good"    },
  avg_junk_rate_pct:   { up: "bad",     down: "good"    },
};

const TREND_LABELS = {
  spend_usd:           "Spend",
  confirmed_sqls:      "SQLs",
  confirmed_waste_usd: "Waste Spend",
  avg_junk_rate_pct:   "Avg Junk Rate",
};

function _trendDirectionClass(key, trend) {
  if (trend === "flat")              return "trend-neutral";
  if (trend === "insufficient_data") return "trend-neutral";
  const map = TREND_DIRECTION[key] || { up: "neutral", down: "neutral" };
  const valence = trend === "up" ? map.up : map.down;
  if (valence === "good")    return "trend-good";
  if (valence === "bad")     return "trend-bad";
  return "trend-neutral";
}

function _trendDirectionLabel(key, trend) {
  if (trend === "insufficient_data") return "No comparison";
  if (trend === "flat")              return "No change";
  const map = TREND_DIRECTION[key] || { up: "neutral", down: "neutral" };
  const valence = trend === "up" ? map.up : map.down;
  if (valence === "good") return "Improved";
  if (valence === "bad")  return "Worsened";
  return trend === "up" ? "Higher" : "Lower";
}

function _fmtMetricValue(key, value) {
  if (value === null || value === undefined) return "—";
  if (key === "confirmed_sqls") return String(value);
  if (key === "avg_junk_rate_pct") return value.toFixed(1) + "%";
  return fmtDollar(value);
}

// Format a delta value for display. Junk-rate delta is in percentage points
// (not %) so it is shown as "+6.5 pts" rather than "+6.5%".
function _fmtMetricDeltaValue(key, value) {
  if (value === null || value === undefined) return "—";
  if (key === "avg_junk_rate_pct") {
    return `${value > 0 ? "+" : ""}${value.toFixed(1)} pts`;
  }
  return (value > 0 ? "+" : "") + _fmtMetricValue(key, value);
}

function renderTrendSummary(summary, hasPrevious, container) {
  if (!container) return;

  const metricKeys = ["spend_usd", "confirmed_sqls", "confirmed_waste_usd", "avg_junk_rate_pct"];

  const noDataNote = !hasPrevious
    ? `<p class="trend-no-previous-note">Not enough previous-period data yet. Current values are shown without comparison.</p>`
    : "";

  const cards = metricKeys.map((key) => {
    const m = summary[key];
    if (!m) return "";
    const dirCls   = _trendDirectionClass(key, m.trend);
    const dirLabel = _trendDirectionLabel(key, m.trend);
    const curVal   = _fmtMetricValue(key, m.current);
    const prevVal  = _fmtMetricValue(key, m.previous);

    // Delta display — junk-rate delta is percentage points, others use value formatter
    let deltaStr = null;
    if (m.delta !== null && m.delta !== undefined) {
      const deltaFmt = _fmtMetricDeltaValue(key, m.delta);
      const deltaPctFmt = (m.delta_pct !== null && key !== "avg_junk_rate_pct")
        ? ` / ${m.delta_pct > 0 ? "+" : ""}${m.delta_pct.toFixed(1)}%`
        : "";
      deltaStr = deltaFmt + deltaPctFmt;
    }

    const arrowUp   = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="18 15 12 9 6 15"/></svg>`;
    const arrowDown = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg>`;
    const arrowIcon = m.trend === "up" ? arrowUp : (m.trend === "down" ? arrowDown : "");

    return `
      <div class="trend-card">
        <div class="trend-card__label">${escapeHtml(TREND_LABELS[key] || key)}</div>
        <div class="trend-card__current">${escapeHtml(curVal)}</div>
        <div class="trend-card__comparison">vs. ${escapeHtml(prevVal)} previous</div>
        ${deltaStr ? `<div class="trend-delta ${dirCls}">${arrowIcon}<span>${escapeHtml(deltaStr)}</span></div>` : ""}
        <div class="trend-direction-badge ${dirCls}">${escapeHtml(dirLabel)}</div>
      </div>`;
  }).join("");

  container.innerHTML = `${noDataNote}<div class="dashboard-trends">${cards}</div>`;
}

function renderCampaignMovements(movements, hasPrevious, container) {
  if (!container) return;

  if (movements.length === 0) {
    if (!hasPrevious) {
      container.innerHTML = `<p class="empty-state" style="padding:var(--space-5)">Not enough previous-period data yet. Trends will appear after more runs.</p>`;
    } else {
      container.innerHTML = `<p class="empty-state" style="padding:var(--space-5)">No campaign movement detected in this period.</p>`;
    }
    return;
  }

  const rows = movements.map((m) => {
    const cur  = m.current  || {};
    const prev = m.previous || {};
    const name = m.campaign_name || "—";
    const mv   = m.movement || "stable";

    const mvCls = {
      improved:          "movement-improved",
      worsened:          "movement-worsened",
      stable:            "movement-stable",
      new:               "movement-new",
      dropped:           "movement-dropped",
      insufficient_data: "movement-insufficient",
    }[mv] || "movement-stable";

    const mvLabel = {
      improved:          "Improved",
      worsened:          "Worsened",
      stable:            "Stable",
      new:               "New",
      dropped:           "Dropped",
      insufficient_data: "Insufficient data",
    }[mv] || mv;

    const spend    = cur.spend_usd      != null ? fmtDollar(cur.spend_usd)     : "—";
    const sqls     = cur.confirmed_sqls != null ? String(cur.confirmed_sqls)   : "—";
    const junk     = cur.junk_rate_pct  != null ? cur.junk_rate_pct.toFixed(1) + "%" : "—";
    const verdict  = cur.verdict        || "";

    const prevSpend  = prev.spend_usd      != null ? fmtDollar(prev.spend_usd)     : "—";
    const prevSqls   = prev.confirmed_sqls != null ? String(prev.confirmed_sqls)   : "—";
    const prevJunk   = prev.junk_rate_pct  != null ? prev.junk_rate_pct.toFixed(1) + "%" : "—";

    // Investigate button if campaign name is available
    const investigateBtn = name && name !== "—"
      ? `<button class="investigate-button movement-investigate-btn" type="button" data-campaign="${escapeHtml(name)}">Investigate</button>`
      : "";

    return `
      <div class="campaign-movement-item">
        <div class="campaign-movement-item__header">
          <span class="campaign-movement-item__name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
          <span class="movement-badge ${mvCls}">${escapeHtml(mvLabel)}</span>
          ${verdictBadge(verdict)}
          ${investigateBtn}
        </div>
        <div class="campaign-movement-item__metrics">
          <span class="movement-metric"><span class="movement-metric__label">Spend</span> <span class="movement-metric__val">${escapeHtml(spend)}</span> <span class="movement-metric__prev">(was ${escapeHtml(prevSpend)})</span></span>
          <span class="movement-metric"><span class="movement-metric__label">SQLs</span> <span class="movement-metric__val">${escapeHtml(sqls)}</span> <span class="movement-metric__prev">(was ${escapeHtml(prevSqls)})</span></span>
          <span class="movement-metric"><span class="movement-metric__label">Junk</span> <span class="movement-metric__val">${escapeHtml(junk)}</span> <span class="movement-metric__prev">(was ${escapeHtml(prevJunk)})</span></span>
        </div>
        ${m.reason ? `<div class="campaign-movement-item__reason">${escapeHtml(m.reason)}</div>` : ""}
      </div>`;
  }).join("");

  container.innerHTML = `<div class="campaign-movement-list">${rows}</div>`;

  // Wire up Investigate buttons
  container.querySelectorAll(".movement-investigate-btn").forEach((btn) => {
    btn.addEventListener("click", () => openCampaignDrawer(btn.dataset.campaign));
  });
}

function renderTrendAlerts(alerts) {
  const el = document.getElementById("dash-alerts-body");
  if (!el) return;

  if (alerts.length === 0) {
    el.innerHTML = `<p class="empty-state">No active alerts.</p>`;
    return;
  }

  const severityIcon = (sev) => {
    if (sev === "high") {
      return `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" class="alert-icon alert-icon--cut"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`;
    }
    return `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" class="alert-icon alert-icon--fix"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`;
  };

  el.innerHTML = alerts.map((a) => `
    <div class="alert-item">
      ${severityIcon(a.severity)}
      <div class="alert-text">
        <span class="alert-severity-badge alert-severity-${escapeHtml(a.severity || "low")}">${escapeHtml((a.severity || "low").toUpperCase())}</span>
        <span class="alert-campaign">${escapeHtml(a.campaign_name || "")}</span>
        — <strong>${escapeHtml(a.title || "")}</strong>
        <div class="alert-detail">${escapeHtml(a.detail || "")}</div>
      </div>
    </div>`).join("");
}

// ── Run history timeline ───────────────────────────────────────────────────

async function loadRunHistory() {
  const el = document.getElementById("run-history-timeline");
  if (!el) return;

  el.innerHTML = `<p class="empty-state">Loading run history…</p>`;

  try {
    const data = await fetchJSON(`/api/runs?days=${getSelectedDays()}`);

    if (data.db_unavailable) {
      // Attempt JSONL fallback so the panel isn't completely empty during a DB outage.
      let fallbackHtml = "";
      try {
        const fallback = await fetchJSON("/runs/latest");
        if (fallback && fallback.status !== "empty" && fallback.run_type) {
          fallbackHtml = renderRunHistoryItem(fallback);
        }
      } catch (_) { /* no JSONL fallback available */ }

      el.innerHTML = (fallbackHtml
        ? `<p class="empty-state" style="margin-bottom:var(--space-3)">Showing latest run from runtime log (database offline).</p>${fallbackHtml}`
        : `<p class="empty-state">Run history temporarily unavailable — database offline.</p>`
      );
      return;
    }

    const runs = (data.runs || []).slice(0, 10);
    if (runs.length === 0) {
      el.innerHTML = `<p class="empty-state">No runs found in the selected window.</p>`;
      return;
    }

    el.innerHTML = runs.map(renderRunHistoryItem).join("");
  } catch (_) {
    el.innerHTML = `<p class="empty-state">Could not load run history.</p>`;
  }
}

function renderRunHistoryItem(run) {
  const status   = normalizeRunStatus(run);
  const dotCls   = status === "success" ? "run-entry__dot--success"
                 : status === "failed"  ? "run-entry__dot--failed"
                 : "run-entry__dot--empty";
  const badgeCls = status === "success" ? "run-status-success"
                 : status === "failed"  ? "run-status-failed"
                 : status === "running" ? "run-status-running"
                 : "";

  const timeStr = run.started_at && run.finished_at
    ? `${fmtDate(run.started_at)} → ${fmtDate(run.finished_at)}`
    : fmtDate(run.started_at || run.finished_at);

  const reportPart = run.report_path
    ? `<span class="run-meta">${escapeHtml((run.report_path.split("/").pop()) || run.report_path)}</span>`
    : "";

  return `
    <div class="run-history-item">
      <div class="run-entry__dot ${dotCls}"></div>
      <div class="run-entry__meta">
        <div class="run-history-item__header">
          <span class="run-entry__type">${fmt(run.run_type)} run</span>
          <span class="run-status-badge ${badgeCls}">${escapeHtml(status)}</span>
        </div>
        <div class="run-entry__time">${timeStr}</div>
        ${reportPart}
      </div>
    </div>`;
}

// ── Campaigns page ─────────────────────────────────────────────────────────

async function loadCampaigns() {
  renderPageDatasetFreshness("campaigns");
  const tableEl = document.getElementById("camp-table-body");
  const scaleEl = document.getElementById("vc-scale");
  const fixEl   = document.getElementById("vc-fix");
  const holdEl  = document.getElementById("vc-hold");
  const cutEl   = document.getElementById("vc-cut");

  if (tableEl) tableEl.innerHTML =
    `<p class="empty-state" style="padding:var(--space-5)">Loading campaigns…</p>`;

  try {
    const data = await fetchJSON(`/api/campaigns?days=${getSelectedDays()}`);
    const campaigns = data.campaigns || [];

    if (campaigns.length === 0) {
      if (tableEl) tableEl.innerHTML =
        `<p class="empty-state" style="padding:var(--space-5)">No campaign data. Trigger a weekly run.</p>`;
      ["vc-scale", "vc-fix", "vc-hold", "vc-cut"].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.textContent = "0";
      });
      return;
    }

    // Count verdicts
    let nScale = 0, nFix = 0, nHold = 0, nCut = 0;
    campaigns.forEach((c) => {
      const v = (c.latest_verdict || "").toUpperCase();
      if (v === "SCALE")     nScale++;
      else if (v === "FIX")  nFix++;
      else if (v === "HOLD") nHold++;
      else if (v === "CUT")  nCut++;
    });

    if (scaleEl) scaleEl.textContent = String(nScale);
    if (fixEl)   fixEl.textContent   = String(nFix);
    if (holdEl)  holdEl.textContent  = String(nHold);
    if (cutEl)   cutEl.textContent   = String(nCut);

    // Sort by spend desc, null spend goes to bottom
    const sorted = [...campaigns].sort((a, b) =>
      (b.avg_spend_usd || 0) - (a.avg_spend_usd || 0)
    );

    const thead = `
      <thead>
        <tr>
          <th>Campaign</th>
          <th class="td--num">Spend (avg/run)</th>
          <th class="td--num">Leads</th>
          <th class="td--num">SQLs</th>
          <th>Junk %</th>
          <th class="td--num">CPQL</th>
          <th>Verdict</th>
          <th class="td--num">Runs</th>
          <th scope="col"><span class="sr-only">Actions</span></th>
        </tr>
      </thead>`;

    const tbody = sorted.map((c) => {
      const v       = (c.latest_verdict || "").toUpperCase();
      const junkPct = c.avg_junk_rate_pct;
      const junkCls = junkPct == null ? "" :
                      junkPct < uiThresholds.junk_rate.low_pct   ? "junk--low" :
                      junkPct <= uiThresholds.junk_rate.high_pct ? "junk--mid" : "junk--high";
      const junkStr = junkPct != null ? junkPct.toFixed(1) + "%" : "—";
      const cpql    = c.total_confirmed_sqls === 0 ? "N/A" :
                      c.avg_cpql_usd != null ? fmtDollar(c.avg_cpql_usd) : "—";
      const spend   = c.avg_spend_usd != null ? fmtDollar(c.avg_spend_usd) : "—";

      return `
        <tr>
          <td class="td--name">${escapeHtml(c.campaign_name || "—")}</td>
          <td class="td--num">${spend}</td>
          <td class="td--num">${c.total_leads != null ? String(c.total_leads) : "—"}</td>
          <td class="td--num">${c.total_confirmed_sqls != null ? String(c.total_confirmed_sqls) : "0"}</td>
          <td class="${junkCls}">${junkStr}</td>
          <td class="td--num ${cpql === "N/A" ? "td--na" : ""}">${cpql}</td>
          <td>${verdictBadge(v)}</td>
          <td class="td--num">${c.run_count != null ? String(c.run_count) : "—"}</td>
          <td><button class="investigate-button" type="button" data-campaign="${escapeHtml(c.campaign_name || "")}">Investigate</button></td>
        </tr>`;
    }).join("");

    if (tableEl) {
      tableEl.innerHTML =
        `<table class="data-table">${thead}<tbody>${tbody}</tbody></table>`;
      // Wire up Investigate buttons — each opens the campaign detail drawer
      tableEl.querySelectorAll(".investigate-button").forEach((btn) => {
        btn.addEventListener("click", () => openCampaignDrawer(btn.dataset.campaign));
      });
    }

  } catch (_) {
    if (tableEl) tableEl.innerHTML =
      `<p class="empty-state" style="padding:var(--space-5)">Could not load campaign data.</p>`;
  }
}

// ── Waste Terms page ───────────────────────────────────────────────────────

let _wasteData = [];  // raw API response, reset on each load

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
  const days = getSelectedDays();

  const tableEl = document.getElementById("waste-table-body");
  if (tableEl) tableEl.innerHTML =
    `<p class="empty-state" style="padding:var(--space-5)">Loading waste terms…</p>`;

  ["waste-kpi-spend", "waste-kpi-terms", "waste-kpi-campaigns", "waste-kpi-crm"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.textContent = "—";
  });

  try {
    const data = await fetchJSON(`/api/waste?days=${days}`);
    _wasteData = data.waste || [];

    populateWasteFilters(_wasteData);
    renderWasteKPIs(_wasteData);
    applyWasteFilters();

  } catch (_) {
    _wasteData = [];
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
      tableEl.innerHTML = `
        <div class="waste-empty-state">
          <p class="empty-state">No flagged waste terms in this time range.</p>
          <p class="waste-empty-subtext">This does not mean there was no waste. It means no terms crossed the current detection rules for the selected window.</p>
        </div>`;
    } else {
      tableEl.innerHTML =
        `<p class="empty-state" style="padding:var(--space-5)">No results match the current filter.</p>`;
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

  tableEl.innerHTML = `<table class="data-table">${thead}<tbody>${tbody}</tbody></table>`;
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
    const data  = await fetchJSON(`/api/leads?days=${getSelectedDays()}`);
    // Deduplicate by contact_id — leads endpoint returns one row per run per lead.
    // Rows without contact_id are kept individually (not collapsed under null key).
    const seen  = new Map();
    for (const [index, lead] of (data.leads || []).entries()) {
      const dedupeKey = hasValidContactId(lead)
        ? `contact:${lead.contact_id}`
        : `row:${index}`;
      const existing = seen.get(dedupeKey);
      if (!existing || lead.run_date > existing.run_date) {
        seen.set(dedupeKey, lead);
      }
    }
    const leads = Array.from(seen.values());

    if (leads.length === 0) {
      if (tableEl) tableEl.innerHTML =
        `<p class="empty-state" style="padding:var(--space-5)">No lead data yet. Trigger a weekly run.</p>`;
      [totalEl, sqlsEl, junkEl, progressEl].forEach((el) => {
        if (el) el.textContent = "—";
      });
      return;
    }

    // Aggregate KPIs
    let sumTotal = 0, sumSQL = 0, sumJunk = 0, sumProgress = 0;
    leads.forEach((l) => {
      const cat = l.status_category || "unknown";
      sumTotal++;
      if (cat === "qualified")   sumSQL++;
      if (cat === "junk")        sumJunk++;
      if (cat === "in_progress") sumProgress++;
    });

    if (totalEl)    totalEl.textContent    = String(sumTotal);
    if (sqlsEl)     sqlsEl.textContent     = String(sumSQL);
    if (junkEl)     junkEl.textContent     = String(sumJunk);
    if (progressEl) progressEl.textContent = String(sumProgress);

    // Group by campaign for per-campaign breakdown
    const byCampaign = new Map();
    leads.forEach((l) => {
      const name = l.campaign_name || "(unknown)";
      if (!byCampaign.has(name)) {
        byCampaign.set(name, { total: 0, sql: 0, progress: 0, junk: 0, wrong_fit: 0, unknown: 0 });
      }
      const g = byCampaign.get(name);
      g.total++;
      const cat = l.status_category || "unknown";
      if (cat === "qualified")   g.sql++;
      if (cat === "in_progress") g.progress++;
      if (cat === "junk")        g.junk++;
      if (cat === "wrong_fit")   g.wrong_fit++;
      if (cat === "unknown")     g.unknown++;
    });

    // Sort by total leads desc
    const rows = Array.from(byCampaign.entries())
      .sort((a, b) => b[1].total - a[1].total);

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

    const tbody = rows.map(([name, g]) => {
      const junkPct = g.total > 0 ? Math.round((g.junk / g.total) * 100) : 0;
      const barCls  = junkPct < uiThresholds.junk_rate.low_pct   ? "progress-bar__fill--low" :
                      junkPct <= uiThresholds.junk_rate.high_pct ? "progress-bar__fill--mid" : "progress-bar__fill--high";
      const junkCls = junkPct < uiThresholds.junk_rate.low_pct   ? "junk--low" :
                      junkPct <= uiThresholds.junk_rate.high_pct ? "junk--mid" : "junk--high";
      return `
        <tr>
          <td class="td--name">${escapeHtml(name)}</td>
          <td class="td--num">${g.total}</td>
          <td class="td--num">${g.sql}</td>
          <td class="td--num">${g.progress}</td>
          <td class="td--num">${g.junk}</td>
          <td class="td--num">${g.wrong_fit}</td>
          <td class="td--num">${g.unknown}</td>
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

async function loadDeals() {
  renderPageDatasetFreshness("deals");
  const funnelEl = document.getElementById("deals-funnel-body");
  const tableEl  = document.getElementById("deals-table-body");
  const EMPTY    = "No GCLID-matched deals found yet. Deals appear here once HubSpot deal attribution is active.";

  if (funnelEl) funnelEl.innerHTML = `<p class="empty-state">${EMPTY}</p>`;
  if (tableEl)  tableEl.innerHTML  = `<p class="empty-state" style="padding:var(--space-5)">${EMPTY}</p>`;

  try {
    const data  = await fetchJSON(`/api/deals?days=${getSelectedDays()}`);
    const deals = data.deals || [];

    if (deals.length === 0) return; // Empty state already set

    // Count by stage
    const stageCounts = {};
    DEAL_PIPELINE_STAGES.forEach((s) => { stageCounts[s] = 0; });
    deals.forEach((d) => {
      // Use deal_stage (raw DB value) for pipeline stage matching
      const stage = d.deal_stage || "";
      const match = DEAL_PIPELINE_STAGES.find((s) =>
        stage.toLowerCase().includes(s.toLowerCase())
      );
      if (match) stageCounts[match]++;
    });

    const maxCount = Math.max(...Object.values(stageCounts), 1);

    if (funnelEl) {
      funnelEl.innerHTML = `
        <div class="funnel">
          ${DEAL_PIPELINE_STAGES.map((s) => {
            const count = stageCounts[s];
            const w     = Math.max(30, Math.round((count / maxCount) * 400));
            return `
              <div class="funnel-stage">
                <div class="funnel-stage__label">${escapeHtml(s)}</div>
                <div class="funnel-stage__bar" style="width:${w}px">
                  <span class="funnel-stage__count">${count}</span>
                </div>
              </div>`;
          }).join("")}
        </div>`;
    }

    const thead = `
      <thead>
        <tr>
          <th>Company</th>
          <th>Country</th>
          <th>Stage</th>
          <th class="td--num">Amount</th>
          <th>Campaign</th>
          <th>Keyword</th>
        </tr>
      </thead>`;

    const tbody = deals.map((d) => {
      const isWon = (d.deal_stage || "").toLowerCase().includes("won");
      return `
        <tr${isWon ? ' class="row--won"' : ""}>
          <td class="td--name">${escapeHtml(d.company || "—")}</td>
          <td>${escapeHtml(d.country || "—")}</td>
          <td>${escapeHtml(d.deal_stage_label || d.deal_stage || "—")}</td><!-- prefer human-readable label -->
          <td class="td--num">${d.deal_amount_usd != null ? fmtDollar(d.deal_amount_usd) : "—"}</td>
          <td>${escapeHtml(d.campaign_name || "—")}</td>
          <td>${escapeHtml(d.keyword || "—")}</td>
        </tr>`;
    }).join("");

    if (tableEl) tableEl.innerHTML =
      `<table class="data-table">${thead}<tbody>${tbody}</tbody></table>`;

  } catch (_) {
    // Empty state already set — silently fail
  }
}

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

  const days = getSelectedDays();

  // Fetch both endpoints; partial failures are tolerated.
  let perfData  = null;
  let leadsData = null;

  try {
    perfData = await fetchJSON(`/api/geo?days=${days}`);
  } catch (_) { /* geo performance unavailable */ }

  try {
    leadsData = await fetchJSON(`/api/leads/country-summary?days=${days}`);
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
        <p class="geo-empty-subtext">Geo data appears after a weekly or monthly run writes Windsor country performance to the database.</p>
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

// ── Search Terms Forensics page ────────────────────────────────────────────

let searchTermsSummary = null;
let searchTermsSummaryStatus = "idle"; // idle | loading | ok | db_unavailable | error
let _searchTermsSummaryReqId = 0;

function buildSearchTermsParams({ cursor = null } = {}) {
  const params = new URLSearchParams();
  params.set("days", String(getSelectedDays()));
  params.set("limit", "100");

  const q         = document.getElementById("search-terms-query")?.value.trim();
  const campaign  = document.getElementById("search-terms-campaign")?.value.trim();
  const matchType = document.getElementById("search-terms-match-type")?.value;
  const minSpend  = document.getElementById("search-terms-min-spend")?.value;
  const wasteFilter = document.getElementById("search-terms-waste-filter")?.value;

  if (q)         params.set("q", q);
  if (campaign)  params.set("campaign", campaign);
  if (matchType) params.set("match_type", matchType);
  if (minSpend)  params.set("min_spend", minSpend);

  if (wasteFilter === "waste") {
    params.set("waste_state", "flagged");
  } else if (wasteFilter === "clean") {
    params.set("waste_state", "clean");
  } else if (wasteFilter === "unanalyzed") {
    params.set("waste_state", "unanalyzed");
  }

  if (cursor) params.set("cursor", cursor);

  return params;
}

function buildSearchTermsSummaryParams() {
  const params = buildSearchTermsParams({ cursor: null });
  params.delete("limit");
  params.delete("cursor");
  return params;
}

async function loadSearchTermsSummary() {
  _searchTermsSummaryReqId += 1;
  const reqId = _searchTermsSummaryReqId;

  searchTermsSummaryStatus = "loading";
  renderSearchTermsKPIs();

  try {
    const params = buildSearchTermsSummaryParams();
    const data = await fetchJSON(`/api/search-terms/summary?${params.toString()}`);

    // Discard response if a newer request has already been dispatched.
    if (reqId !== _searchTermsSummaryReqId) return;

    if (data.db_unavailable) {
      searchTermsSummaryStatus = "db_unavailable";
      searchTermsSummary = data;
    } else {
      searchTermsSummaryStatus = "ok";
      searchTermsSummary = data;
    }

    renderSearchTermsKPIs();
  } catch (err) {
    if (reqId !== _searchTermsSummaryReqId) return;
    searchTermsSummaryStatus = "error";
    searchTermsSummary = null;
    renderSearchTermsKPIs();
  }
}

function getVisibleSearchTermRows() {
  // Analysis-state filtering is backend-owned (PR-ADS-052).
  // This helper filters only client-side visibility flags (hidden/visible) so
  // downstream "no results match filter" branches stay reachable for future
  // client-side visibility features without reintroducing waste-state filtering.
  return (Array.isArray(searchTermRows) ? searchTermRows : []).filter((row) => {
    if (!row || typeof row !== "object") return true;
    if (row.hidden === true) return false;
    if (row._hidden === true) return false;
    if (row.visible === false) return false;
    return true;
  });
}

function _updateSearchTermsFilterNote() {
  // As of PR-ADS-052, analysis-state filters are backend-applied, so no client-side caveat
  // is needed. The note element is kept in the DOM but permanently hidden so it can be
  // repurposed for future informational messages without an HTML change.
  const noteEl = document.getElementById("search-terms-filter-note");
  if (!noteEl) return;
  noteEl.hidden = true;
}

function _updateSearchTermsPaginationVisibility() {
  const container = document.querySelector("#page-search-terms .pagination-actions");
  const btn       = document.getElementById("search-terms-load-more-btn");
  if (!container || !btn) return;
  container.hidden = btn.hidden;
}

async function loadSearchTerms({ reset = false, cursor = null } = {}) {
  if (searchTermsStatus === "loading") return;

  if (reset) renderPageDatasetFreshness("search_terms");

  searchTermsStatus = "loading";

  const tableEl   = document.getElementById("search-terms-table");
  const loadMoreBtn = document.getElementById("search-terms-load-more-btn");

  if (reset) {
    searchTermRows = [];
    searchTermsNextCursor = null;
    searchTermsHasMore = false;
    // Reset summary state and fire summary loader in parallel with the table load.
    searchTermsSummary = null;
    searchTermsSummaryStatus = "idle";
    renderSearchTermsKPIs();
    loadSearchTermsSummary();
    if (tableEl) tableEl.innerHTML =
      `<p class="empty-state" style="padding:var(--space-5)">Loading search terms…</p>`;
    if (loadMoreBtn) loadMoreBtn.hidden = true;
    _updateSearchTermsPaginationVisibility();
  } else {
    if (loadMoreBtn) {
      loadMoreBtn.disabled = true;
      loadMoreBtn.textContent = "Loading…";
    }
  }

  try {
    const params = buildSearchTermsParams({ cursor });
    const data   = await fetchJSON(`/api/search-terms?${params.toString()}`);

    if (data.db_unavailable) {
      searchTermsStatus = "db_unavailable";
      searchTermRows = [];
      renderSearchTermsTable();
      if (loadMoreBtn) loadMoreBtn.hidden = true;
      _updateSearchTermsPaginationVisibility();
      return;
    }

    const newRows = data.rows || [];
    const pagination = data.pagination || {};

    if (reset) {
      searchTermRows = newRows;
    } else {
      searchTermRows = searchTermRows.concat(newRows);
    }

    searchTermsNextCursor = pagination.next_cursor || null;
    searchTermsHasMore    = pagination.has_more || false;

    if (searchTermRows.length === 0) {
      searchTermsStatus = "empty";
    } else {
      searchTermsStatus = "ok";
    }

    renderSearchTermsTable();

    if (loadMoreBtn) {
      if (searchTermsHasMore) {
        loadMoreBtn.hidden   = false;
        loadMoreBtn.disabled = false;
        loadMoreBtn.textContent = "Load more";
      } else {
        loadMoreBtn.hidden = true;
      }
    }
    _updateSearchTermsPaginationVisibility();

  } catch (err) {
    // Distinguish invalid cursor (HTTP 400) from generic error
    const isCursorError = err && err.message && err.message.includes("Invalid cursor");
    searchTermsStatus = isCursorError ? "cursor_error" : "error";
    renderSearchTermsTable();
    // On cursor errors: hide Load More — retrying with the same broken cursor won't help.
    // On generic errors with no valid next cursor: also hide.
    if (loadMoreBtn) {
      const canLoadMore = searchTermsStatus !== "cursor_error" && !!searchTermsNextCursor;
      loadMoreBtn.hidden   = !canLoadMore;
      loadMoreBtn.disabled = false;
      loadMoreBtn.textContent = "Load more";
    }
    _updateSearchTermsPaginationVisibility();
  }
}

function renderSearchTermsKPIs() {
  const kpiGrid = document.getElementById("search-terms-kpis");
  if (!kpiGrid) return;

  // Loading state — show skeleton cards
  if (searchTermsSummaryStatus === "loading" || searchTermsSummaryStatus === "idle") {
    kpiGrid.innerHTML = `
      <div class="kpi-card">
        <div class="kpi-card__label">Matching Terms</div>
        <div class="kpi-card__value">…</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-card__label">Matching Spend</div>
        <div class="kpi-card__value">…</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-card__label">Flagged Waste</div>
        <div class="kpi-card__value">…</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-card__label">Unanalyzed</div>
        <div class="kpi-card__value">…</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-card__label">Avg CPC</div>
        <div class="kpi-card__value">…</div>
      </div>`;
    return;
  }

  // DB unavailable state
  if (searchTermsSummaryStatus === "db_unavailable") {
    kpiGrid.innerHTML = `
      <div class="kpi-card">
        <div class="kpi-card__label">Matching Terms</div>
        <div class="kpi-card__value">—</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-card__label">Matching Spend</div>
        <div class="kpi-card__value">—</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-card__label">Flagged Waste</div>
        <div class="kpi-card__value">—</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-card__label">Unanalyzed</div>
        <div class="kpi-card__value">—</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-card__label">Avg CPC</div>
        <div class="kpi-card__value">—</div>
        <div class="kpi-subtext">Search-term summary unavailable — database offline.</div>
      </div>`;
    return;
  }

  // Error state
  if (searchTermsSummaryStatus === "error") {
    kpiGrid.innerHTML = `
      <div class="kpi-card">
        <div class="kpi-card__label">Matching Terms</div>
        <div class="kpi-card__value">—</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-card__label">Matching Spend</div>
        <div class="kpi-card__value">—</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-card__label">Flagged Waste</div>
        <div class="kpi-card__value">—</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-card__label">Unanalyzed</div>
        <div class="kpi-card__value">—</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-card__label">Avg CPC</div>
        <div class="kpi-card__value">—</div>
        <div class="kpi-subtext">Could not load search-term summary.</div>
      </div>`;
    return;
  }

  // Success — render from backend summary
  const s     = searchTermsSummary?.summary     || {};
  const state = searchTermsSummary?.analysis_state || {};

  const flaggedRows    = state.flagged?.rows    ?? 0;
  const flaggedSpend   = state.flagged?.spend_usd ?? 0;
  const unanalyzedRows  = state.unanalyzed?.rows  ?? 0;
  const unanalyzedSpend = state.unanalyzed?.spend_usd ?? 0;
  const avgCPC         = s.avg_cpc_usd ?? null;

  kpiGrid.innerHTML = `
    <div class="kpi-card">
      <div class="kpi-card__label">Matching Terms</div>
      <div class="kpi-card__value">${s.total_terms ?? 0}</div>
      <div class="kpi-subtext">${s.unique_search_terms ?? 0} unique</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">Matching Spend</div>
      <div class="kpi-card__value">${fmtDollar(s.total_spend_usd ?? 0)}</div>
      <div class="kpi-subtext">${s.total_clicks ?? 0} clicks</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">Flagged Waste</div>
      <div class="kpi-card__value">${flaggedRows}</div>
      <div class="kpi-subtext">${fmtDollar(flaggedSpend)}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">Unanalyzed</div>
      <div class="kpi-card__value">${unanalyzedRows}</div>
      <div class="kpi-subtext">${fmtDollar(unanalyzedSpend)}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">Avg CPC</div>
      <div class="kpi-card__value">${avgCPC !== null ? "$" + Number(avgCPC).toFixed(2) : "—"}</div>
      <div class="kpi-subtext">${s.total_clicks ?? 0} clicks</div>
    </div>`;
}

function searchTermStateBadge(isFlaggedWaste) {
  if (isFlaggedWaste === true) {
    return `<span class="search-term-state-badge search-term-state-waste">Flagged Waste</span>`;
  }
  if (isFlaggedWaste === false) {
    return `<span class="search-term-state-badge search-term-state-clean">Analyzed Clean</span>`;
  }
  return `<span class="search-term-state-badge search-term-state-unanalyzed">Unanalyzed</span>`;
}

function renderSearchTermsTable() {
  const tableEl = document.getElementById("search-terms-table");
  if (!tableEl) return;

  if (searchTermsStatus === "db_unavailable") {
    tableEl.innerHTML = `
      <div class="waste-empty-state">
        <p class="empty-state">Search terms temporarily unavailable — database offline.</p>
      </div>`;
    return;
  }

  if (searchTermsStatus === "cursor_error") {
    tableEl.innerHTML = `
      <div class="waste-empty-state">
        <p class="empty-state">Could not load the next page. Refresh and try again.</p>
      </div>`;
    return;
  }

  if (searchTermsStatus === "error") {
    tableEl.innerHTML = `
      <div class="waste-empty-state">
        <p class="empty-state">Could not load search terms. Check API health or run status.</p>
      </div>`;
    return;
  }

  const visible = getVisibleSearchTermRows();

  if (visible.length === 0) {
    if (searchTermsStatus === "empty" || searchTermRows.length === 0) {
      tableEl.innerHTML = `
        <div class="waste-empty-state">
          <p class="empty-state">No search-term rows are stored for this window.</p>
          <p class="waste-empty-subtext">
            This does not mean the account is clean. It means the Search Terms evidence pipeline
            has no data for the selected window.<br><br>
            <strong>Check:</strong><br>
            1. Scheduler → latest weekly/daily run<br>
            2. System Health → search_terms freshness<br>
            3. Reality Audit → search_terms verdict
          </p>
        </div>`;
    } else {
      tableEl.innerHTML =
        `<p class="empty-state" style="padding:var(--space-5)">No results match the current filter.</p>`;
    }
    return;
  }

  const thead = `
    <thead>
      <tr>
        <th>Source Date</th>
        <th>Search Term</th>
        <th>Waste State</th>
        <th>Campaign</th>
        <th>Ad Group</th>
        <th>Keyword</th>
        <th>Match Type</th>
        <th class="td--num">Spend</th>
        <th class="td--num">Clicks</th>
        <th class="td--num">Impr.</th>
        <th class="td--num">Google Conv.</th>
        <th>Junk Category</th>
        <th>Pattern</th>
      </tr>
    </thead>`;

  const tbody = visible.map((r) => {
    const highSpend = (r.spend_usd || 0) >= uiThresholds.spend.high_spend_usd;
    return `
      <tr${highSpend ? ' class="row--high-spend"' : ""}>
        <td>${escapeHtml(r.source_date || "—")}</td>
        <td class="td--name">${escapeHtml(r.search_term || "—")}</td>
        <td>${searchTermStateBadge(r.is_flagged_waste)}</td>
        <td>${escapeHtml(r.campaign_name || "—")}</td>
        <td>${escapeHtml(r.ad_group || "—")}</td>
        <td>${escapeHtml(r.keyword || "—")}</td>
        <td>${matchTypeBadge(r.match_type)}</td>
        <td class="td--num${highSpend ? " waste-spend--high" : ""}">${r.spend_usd != null ? fmtDollar(r.spend_usd) : "—"}</td>
        <td class="td--num">${r.clicks != null ? r.clicks : "—"}</td>
        <td class="td--num">${r.impressions != null ? r.impressions : "—"}</td>
        <td class="td--num">${r.conversions != null ? r.conversions.toFixed(1) : "—"}</td>
        <td>${r.junk_category ? junkCategoryBadge(r.junk_category) : `<span class="td--na">—</span>`}</td>
        <td class="waste-pattern">${escapeHtml(r.matched_pattern || "—")}</td>
      </tr>`;
  }).join("");

  tableEl.innerHTML = `<div class="search-terms-table-scroll"><table class="data-table">${thead}<tbody>${tbody}</tbody></table></div>`;
}

// ── N-Gram Intelligence page ───────────────────────────────────────────────

let ngramRows = [];
let ngramSummary = null;
let ngramStatus = "idle"; // idle | loading | ok | empty | db_unavailable | error
let ngramRequestId = 0;
let ngramIsLoading = false;

function buildNgramParams() {
  const params = new URLSearchParams();
  params.set("days", String(getSelectedDays()));
  params.set("limit", "100");

  const q         = document.getElementById("ngrams-query")?.value.trim();
  const campaign  = document.getElementById("ngrams-campaign")?.value.trim();
  const matchType = document.getElementById("ngrams-match-type")?.value;
  const wasteState = document.getElementById("ngrams-waste-state")?.value;
  const n         = document.getElementById("ngrams-length")?.value;
  const minSpend  = document.getElementById("ngrams-min-spend")?.value;

  if (q)          params.set("q", q);
  if (campaign)   params.set("campaign", campaign);
  if (matchType)  params.set("match_type", matchType);
  if (wasteState) params.set("waste_state", wasteState);
  if (n)          params.set("n", n);
  if (minSpend)   params.set("min_spend", minSpend);

  return params;
}

async function loadNgrams() {
  if (ngramIsLoading) return;
  renderPageDatasetFreshness("ngrams");
  ngramIsLoading = true;
  const requestId = ++ngramRequestId;
  ngramStatus = "loading";
  setNgramControlsDisabled(true);
  renderNgramsKPIs();
  renderNgramsTable();

  try {
    const params = buildNgramParams();
    const data = await fetchJSON(`/api/search-terms/ngrams?${params.toString()}`);

    if (requestId !== ngramRequestId) return;

    if (data.db_unavailable) {
      ngramStatus = "db_unavailable";
      ngramRows = [];
      ngramSummary = data.summary || null;
      renderNgramsKPIs(data);
      renderNgramsTable();
      return;
    }

    ngramRows   = data.rows    || [];
    ngramSummary = data.summary || null;
    ngramStatus = ngramRows.length ? "ok" : "empty";

    renderNgramsKPIs(data);
    renderNgramsTable(data);
  } catch (err) {
    if (requestId !== ngramRequestId) return;
    ngramStatus  = "error";
    ngramRows    = [];
    ngramSummary = null;
    renderNgramsKPIs();
    renderNgramsTable();
  } finally {
    if (requestId === ngramRequestId) {
      ngramIsLoading = false;
      setNgramControlsDisabled(false);
    }
  }
}

function setNgramControlsDisabled(disabled) {
  const applyBtn = document.getElementById("ngrams-apply-btn");
  if (applyBtn) {
    applyBtn.disabled = disabled;
    applyBtn.textContent = disabled ? "Loading…" : "Apply filters";
  }

  [
    "ngrams-refresh-btn",
    "ngrams-clear-btn",
    "ngrams-query",
    "ngrams-campaign",
    "ngrams-match-type",
    "ngrams-waste-state",
    "ngrams-length",
    "ngrams-min-spend",
  ].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.disabled = disabled;
  });
}

function clearNgramFilters() {
  ["ngrams-query", "ngrams-campaign", "ngrams-min-spend"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.value = "";
  });

  const matchType = document.getElementById("ngrams-match-type");
  if (matchType) matchType.value = "";

  const wasteState = document.getElementById("ngrams-waste-state");
  if (wasteState) wasteState.value = "";

  const n = document.getElementById("ngrams-length");
  if (n) n.value = "1,2,3";

  loadNgrams();
}

function renderNgramDataQualityNote(data) {
  const dq = (data && data.data_quality) || {};
  if (dq.row_cap_applied) {
    return `
      <div class="ngram-data-quality-warning">
        Row cap applied: analysis used the top ${escapeHtml(String(dq.row_cap || "configured"))}
        source rows by spend/date. Treat results as directional. Narrow filters or date range for a more complete view.
      </div>
    `;
  }

  return `
    <div class="ngram-data-quality-note">
      These are candidate patterns for human review only. N-gram results are factual pattern metrics from stored search terms. No negative keywords are pushed and no changes are made to Google Ads.
    </div>
  `;
}

function ngramLanguageBadge(language) {
  const map = {
    arabic:           { label: "Arabic",     cls: "ngram-language-badge--arabic" },
    spanish:          { label: "Spanish",    cls: "ngram-language-badge--spanish" },
    english_or_latin: { label: "EN / Latin", cls: "ngram-language-badge--english" },
  };
  const entry = map[(language || "").toLowerCase()] || { label: escapeHtml(language || "—"), cls: "" };
  return `<span class="ngram-language-badge ${entry.cls}">${entry.label}</span>`;
}

function ngramWasteStateMix(flagged, clean, unanalyzed) {
  const hasValue = (v) => v !== null && v !== undefined && Number.isFinite(Number(v));

  const parts = [];
  if (hasValue(flagged))    parts.push(`<span class="ngram-state-flagged">${escapeHtml(String(Number(flagged)))}F</span>`);
  if (hasValue(clean))      parts.push(`<span class="ngram-state-clean">${escapeHtml(String(Number(clean)))}C</span>`);
  if (hasValue(unanalyzed)) parts.push(`<span class="ngram-state-unanalyzed">${escapeHtml(String(Number(unanalyzed)))}U</span>`);

  return parts.length
    ? `<span class="ngram-state-mix">${parts.join(" / ")}</span>`
    : `<span class="td--na">—</span>`;
}

function renderNgramsKPIs(data) {
  const kpiGrid = document.getElementById("ngrams-kpis");
  if (!kpiGrid) return;

  if (ngramStatus === "loading" || ngramStatus === "idle") {
    kpiGrid.innerHTML = `
      <div class="kpi-card"><div class="kpi-card__label">N-Grams Returned</div><div class="kpi-card__value">…</div></div>
      <div class="kpi-card"><div class="kpi-card__label">Source Rows Analyzed</div><div class="kpi-card__value">…</div></div>
      <div class="kpi-card"><div class="kpi-card__label">Unique Search Terms</div><div class="kpi-card__value">…</div></div>
      <div class="kpi-card"><div class="kpi-card__label">Highest N-Gram Spend</div><div class="kpi-card__value">…</div></div>
      <div class="kpi-card"><div class="kpi-card__label">Row Cap</div><div class="kpi-card__value">…</div></div>`;
    return;
  }

  if (ngramStatus === "db_unavailable") {
    kpiGrid.innerHTML = `
      <div class="kpi-card"><div class="kpi-card__label">N-Grams Returned</div><div class="kpi-card__value">—</div></div>
      <div class="kpi-card"><div class="kpi-card__label">Source Rows Analyzed</div><div class="kpi-card__value">—</div></div>
      <div class="kpi-card"><div class="kpi-card__label">Unique Search Terms</div><div class="kpi-card__value">—</div></div>
      <div class="kpi-card"><div class="kpi-card__label">Highest N-Gram Spend</div><div class="kpi-card__value">—</div></div>
      <div class="kpi-card"><div class="kpi-card__label">Row Cap</div><div class="kpi-card__value">—</div><div class="kpi-subtext">N-gram analysis temporarily unavailable — database offline.</div></div>`;
    return;
  }

  if (ngramStatus === "error") {
    kpiGrid.innerHTML = `
      <div class="kpi-card"><div class="kpi-card__label">N-Grams Returned</div><div class="kpi-card__value">—</div></div>
      <div class="kpi-card"><div class="kpi-card__label">Source Rows Analyzed</div><div class="kpi-card__value">—</div></div>
      <div class="kpi-card"><div class="kpi-card__label">Unique Search Terms</div><div class="kpi-card__value">—</div></div>
      <div class="kpi-card"><div class="kpi-card__label">Highest N-Gram Spend</div><div class="kpi-card__value">—</div></div>
      <div class="kpi-card"><div class="kpi-card__label">Row Cap</div><div class="kpi-card__value">—</div><div class="kpi-subtext">Could not load n-gram analysis.</div></div>`;
    return;
  }

  const s  = ngramSummary || {};
  const dq = (data && data.data_quality) || {};
  const maxNgramSpend = ngramRows.reduce((max, r) => Math.max(max, Number(r.total_spend_usd || 0)), 0);
  const rowCapLabel = dq.row_cap_applied ? "Applied" : "Not applied";

  kpiGrid.innerHTML = `
    <div class="kpi-card">
      <div class="kpi-card__label">N-Grams Returned</div>
      <div class="kpi-card__value">${s.ngrams_returned ?? ngramRows.length}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">Source Rows Analyzed</div>
      <div class="kpi-card__value">${s.source_rows_analyzed != null ? s.source_rows_analyzed.toLocaleString() : "—"}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">Unique Search Terms</div>
      <div class="kpi-card__value">${s.unique_search_terms_analyzed != null ? s.unique_search_terms_analyzed.toLocaleString() : "—"}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">Highest N-Gram Spend</div>
      <div class="kpi-card__value">${fmtDollar(maxNgramSpend)}</div>
      <div class="kpi-subtext">Top returned n-gram</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">Row Cap</div>
      <div class="kpi-card__value">${escapeHtml(rowCapLabel)}</div>
      ${dq.row_cap_applied && dq.row_cap ? `<div class="kpi-subtext">Cap: ${dq.row_cap.toLocaleString()} rows</div>` : ""}
    </div>`;
}

function renderNgramsTable(data) {
  const tableEl = document.getElementById("ngrams-table");
  if (!tableEl) return;

  if (ngramStatus === "loading" || ngramStatus === "idle") {
    tableEl.innerHTML = `<p class="empty-state" style="padding:var(--space-5)">Loading n-gram patterns…</p>`;
    return;
  }

  if (ngramStatus === "db_unavailable") {
    tableEl.innerHTML = `<div class="waste-empty-state"><p class="empty-state">N-gram analysis temporarily unavailable — database offline.</p></div>`;
    return;
  }

  if (ngramStatus === "error") {
    tableEl.innerHTML = `<div class="waste-empty-state"><p class="empty-state">Could not load n-gram analysis. Check API health or filters.</p></div>`;
    return;
  }

  if (ngramRows.length === 0) {
    tableEl.innerHTML = `<div class="waste-empty-state"><p class="empty-state">No n-grams found for the selected filters.</p></div>`;
    return;
  }

  const thead = `
    <thead>
      <tr>
        <th>N-Gram</th>
        <th class="td--num">N</th>
        <th>Language</th>
        <th class="td--num">Row Count</th>
        <th class="td--num">Unique Terms</th>
        <th class="td--num">Campaigns</th>
        <th class="td--num">Spend</th>
        <th class="td--num">Clicks</th>
        <th class="td--num">Impressions</th>
        <th class="td--num">Google Conv.</th>
        <th>Flagged / Clean / Unanalyzed</th>
        <th class="td--num">Flagged Spend</th>
        <th>Campaign Samples</th>
        <th>Search-Term Samples</th>
      </tr>
    </thead>`;

  const tbody = ngramRows.map((r) => {
    const campaignSamples = (r.campaigns_sample || []).slice(0, 5);
    const termSamples     = (r.search_terms_sample || []).slice(0, 3);

    const campaignChips = campaignSamples.length
      ? campaignSamples.map((c) => `<span class="ngram-chip">${escapeHtml(c)}</span>`).join("")
      : `<span class="td--na">—</span>`;

    const termList = termSamples.length
      ? `<ul class="ngram-sample-list">${termSamples.map((t) => `<li>"${escapeHtml(t)}"</li>`).join("")}</ul>`
      : `<span class="td--na">—</span>`;

    return `
      <tr>
        <td class="td--name">${escapeHtml(r.ngram || "—")}</td>
        <td class="td--num">${r.n != null ? r.n : "—"}</td>
        <td>${ngramLanguageBadge(r.language)}</td>
        <td class="td--num">${r.row_count != null ? r.row_count : "—"}</td>
        <td class="td--num">${r.unique_search_terms != null ? r.unique_search_terms : "—"}</td>
        <td class="td--num">${r.campaigns_count != null ? r.campaigns_count : "—"}</td>
        <td class="td--num">${r.total_spend_usd != null ? fmtDollar(r.total_spend_usd) : "—"}</td>
        <td class="td--num">${r.total_clicks != null ? r.total_clicks : "—"}</td>
        <td class="td--num">${r.total_impressions != null ? r.total_impressions.toLocaleString() : "—"}</td>
        <td class="td--num">${r.google_conversions != null ? r.google_conversions.toFixed(1) : "—"}</td>
        <td>${ngramWasteStateMix(r.flagged_waste_rows, r.clean_rows, r.unanalyzed_rows)}</td>
        <td class="td--num">${r.flagged_waste_spend_usd != null ? fmtDollar(r.flagged_waste_spend_usd) : "—"}</td>
        <td><div class="ngram-chips-wrap">${campaignChips}</div></td>
        <td>${termList}</td>
      </tr>`;
  }).join("");

  tableEl.innerHTML = renderNgramDataQualityNote(data) + `<div class="ngrams-table-scroll"><table class="data-table">${thead}<tbody>${tbody}</tbody></table></div>`;
}

function downloadNgramsCSV() {
  if (!ngramRows.length) return;
  const headers = [
    "N-Gram", "N", "Language", "Row Count", "Unique Search Terms", "Campaigns",
    "Spend USD", "Clicks", "Impressions", "Google Conv.", "Flagged Rows", "Clean Rows",
    "Unanalyzed Rows", "Flagged Spend USD",
  ];
  const rows = ngramRows.map((r) => [
    r.ngram, r.n, r.language, r.row_count, r.unique_search_terms, r.campaigns_count,
    r.total_spend_usd, r.total_clicks, r.total_impressions, r.google_conversions,
    r.flagged_waste_rows, r.clean_rows, r.unanalyzed_rows, r.flagged_waste_spend_usd,
  ]);
  const days = getSelectedDays();
  downloadCSV(`ngrams_${days}d.csv`, headers, rows);
}

function downloadSearchTermsCSV() {
  if (!searchTermRows.length) return;
  const headers = [
    "Source Date", "Search Term", "Waste State", "Campaign", "Ad Group",
    "Keyword", "Match Type", "Spend USD", "Clicks", "Impressions",
    "Google Conv.", "Junk Category", "Matched Pattern",
  ];
  const stateLabel = (v) => v === true ? "flagged" : v === false ? "clean" : "unanalyzed";
  const rows = searchTermRows.map((r) => [
    r.source_date, r.search_term, stateLabel(r.is_flagged_waste),
    r.campaign_name, r.ad_group, r.keyword, r.match_type,
    r.spend_usd, r.clicks, r.impressions, r.conversions,
    r.junk_category || "", r.matched_pattern || "",
  ]);
  const days = getSelectedDays();
  downloadCSV(`search_terms_${days}d.csv`, headers, rows);
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
  const days = getSelectedDays();
  downloadCSV(`waste_terms_${days}d.csv`, headers, rows);
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
  const days = getSelectedDays();

  try {
    const data = await fetchJSON(`/api/keywords?days=${days}`);

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
          <p class="keywords-empty-subtext">Keyword rows appear after weekly or monthly runs persist Windsor keyword performance.</p>
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
    const data  = await fetchJSON(`/api/leads?days=${getSelectedDays()}`);

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

    el.innerHTML = renderGroup("Meeting Booked", booked)
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
    bodyEl.innerHTML = `<p class="empty-state">No run data available.</p>`;
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
      navigate("dashboard");
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

const PAGE_PIPELINE_IMPACT = {
  dashboard: ["campaigns", "leads", "waste_terms"],
  campaigns: ["campaigns"],
  waste: ["waste_terms", "search_terms"],
  "search-terms": ["search_terms"],
  ngrams: ["ngrams", "search_terms"],
  geo: ["geo"],
  keywords: ["keywords"],
  leads: ["leads"],
  deals: ["deals"],
  "gclid-attribution": ["gclid_attribution", "gclid_coverage_snapshots"],
  opportunities: ["leads"],
  health: ["all"],
  backfill: ["admin"],
  "historical-intelligence": ["historical_intelligence"]
};

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
        <td>${escapeHtml(p.source || "")} → ${escapeHtml(p.label || "")}</td>
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

  // Page Impact
  const impacts = data.page_impact || [];
  if (impacts.length > 0) {
    html += `<div class="war-room-section"><h3 class="war-room-section__title">Page Impact</h3><div class="war-room-impacts">`;
    for (const imp of impacts) {
      html += `
        <div class="war-room-impact">
          <span class="war-room-impact__page">${escapeHtml(imp.page)}</span>
          <span class="war-room-impact__status">Blocked</span>
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
    "windsor/search_terms": { page: "search-terms", label: "Search Terms" },
    "windsor/keywords":     { page: "keywords",     label: "Keywords"     },
    "windsor/geo":          { page: "geo",           label: "Geo"          },
    "windsor/campaigns":    { page: "campaigns",     label: "Campaigns"    },
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
    fresh_with_data:      "freshness-status-fresh-with-data",
    fresh_but_empty:      "freshness-status-fresh-but-empty",
    stale_with_data:      "freshness-status-stale-with-data",
    stale_and_empty:      "freshness-status-stale-and-empty",
    failed:               "freshness-status-failed",
    running:              "freshness-status-running",
    not_run:              "freshness-status-not-run",
    dependency_blocked:   "freshness-status-dependency-blocked",
    db_unavailable:       "freshness-status-db-unavailable",
    unknown:              "freshness-status-unknown",
  };
  const _canonicalBadgeLabel = {
    fresh_with_data:      "Fresh with data",
    fresh_but_empty:      "Fresh but empty",
    stale_with_data:      "Stale with data",
    stale_and_empty:      "Stale and empty",
    failed:               "Failed",
    running:              "Running",
    not_run:              "Not run",
    dependency_blocked:   "Blocked",
    db_unavailable:       "DB unavailable",
    unknown:              "Unknown",
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
        <td>${escapeHtml(row.source || "—")}</td>
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
    params.set("days", String(getSelectedDays()));
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
    params.set("days", String(getSelectedDays()));
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
    params.set("days",     String(getSelectedDays()));
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


async function openCampaignDrawer(campaignName) {
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

    const detail = await fetchJSON(
      `/api/campaign-detail?campaign_name=${encodeURIComponent(campaignName)}&days=${getSelectedDays()}`
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
    // campaign summary absent (e.g. daily-pulse window — no campaigns snapshot written)
    // but evidence sections may still have data — render what we have
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
    // Partial render — note missing campaign snapshot at the top
    if (titleEl) titleEl.textContent = data.campaign_name || "";
    // Inject a banner and fall through to the evidence sections below
    bodyEl.innerHTML = "";
    const banner = document.createElement("div");
    banner.className = "drawer-section drawer-section--header";
    banner.innerHTML = `<p class="drawer-source-note" style="color:var(--c-warning)">Campaign snapshot unavailable for this window — related evidence exists.</p>`;
    bodyEl.appendChild(banner);
    // render remaining evidence using partial helpers
    _appendDrawerEvidenceSections(bodyEl, data, null);
    return;
  }

  const camp = data.campaign;
  const lq   = data.lead_quality;
  if (titleEl) titleEl.textContent = camp.campaign_name || data.campaign_name;

  // ── Header summary ─────────────────────────────────────────────────────
  const v = (camp.verdict || "").toUpperCase();
  const headerHtml = `
    <div class="drawer-section drawer-section--header">
      <div class="drawer-header-meta">
        ${verdictBadge(v)}
        <span class="drawer-verdict-reason">${escapeHtml(camp.verdict_reason || "")}</span>
      </div>
      <div class="drawer-source-note">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
        Campaign truth from DB · Last run: ${escapeHtml(camp.last_run_date || "—")} · ${camp.runs != null ? camp.runs : "—"} run${camp.runs === 1 ? "" : "s"} in window
      </div>
    </div>`;

  // ── KPI row ────────────────────────────────────────────────────────────
  const cpqlStr  = camp.confirmed_sqls === 0 ? "N/A" : (camp.cpql_usd != null ? fmtDollar(camp.cpql_usd) : "—");
  const junkStr  = camp.junk_rate_pct  != null ? camp.junk_rate_pct.toFixed(1) + "%" : "—";
  const sqlsHint = lq ? `${lq.confirmed_sqls}` : (camp.confirmed_sqls != null ? String(camp.confirmed_sqls) : "—");
  const kpiHtml = `
    <div class="drawer-section">
      <div class="drawer-kpi-grid">
        <div class="drawer-kpi">
          <div class="drawer-kpi__label">Spend</div>
          <div class="drawer-kpi__value">${camp.spend_usd != null ? fmtDollar(camp.spend_usd) : "—"}</div>
        </div>
        <div class="drawer-kpi">
          <div class="drawer-kpi__label">Leads</div>
          <div class="drawer-kpi__value">${camp.total_leads != null ? camp.total_leads : "—"}</div>
        </div>
        <div class="drawer-kpi">
          <div class="drawer-kpi__label">SQLs</div>
          <div class="drawer-kpi__value">${sqlsHint}</div>
        </div>
        <div class="drawer-kpi">
          <div class="drawer-kpi__label">Junk Rate</div>
          <div class="drawer-kpi__value">${junkStr}</div>
        </div>
        <div class="drawer-kpi">
          <div class="drawer-kpi__label">CPQL</div>
          <div class="drawer-kpi__value">${cpqlStr}</div>
        </div>
        <div class="drawer-kpi">
          <div class="drawer-kpi__label">Google Conv.</div>
          <div class="drawer-kpi__value">${camp.conversions != null ? camp.conversions.toFixed(1) : "—"}</div>
        </div>
      </div>
    </div>`;

  bodyEl.innerHTML = headerHtml + kpiHtml;
  _appendDrawerEvidenceSections(bodyEl, data, lq);
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
        <p class="drawer-source-note">Keyword metrics are Google Ads/Windsor platform metrics only.</p>
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
        ${escapeHtml(src.keywords || "Windsor keyword performance")},
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
      navigate(el.dataset.page);
    });
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        navigate(el.dataset.page);
      }
    });
  });

  // Wire up time range buttons
  document.querySelectorAll(".time-range-btn").forEach((btn) => {
    btn.addEventListener("click", () => setSelectedDays(parseInt(btn.dataset.days, 10)));
  });
  // Sync active state to the stored/default value on page load
  document.querySelectorAll(".time-range-btn").forEach((btn) => {
    btn.classList.toggle("active", parseInt(btn.dataset.days, 10) === _selectedDays);
  });

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

  // Wire up search terms controls
  const stQueryInput    = document.getElementById("search-terms-query");
  const stCampaignInput = document.getElementById("search-terms-campaign");
  const stApplyBtn      = document.getElementById("search-terms-apply-btn");
  const stClearBtn      = document.getElementById("search-terms-clear-btn");
  const stRefreshBtn    = document.getElementById("search-terms-refresh-btn");
  const stLoadMoreBtn   = document.getElementById("search-terms-load-more-btn");
  const stWasteFilter   = document.getElementById("search-terms-waste-filter");
  const stExportBtn     = document.getElementById("search-terms-export-csv-btn");

  if (stApplyBtn) stApplyBtn.addEventListener("click", () => loadSearchTerms({ reset: true }));
  if (stClearBtn) stClearBtn.addEventListener("click", () => {
    if (stQueryInput)    stQueryInput.value    = "";
    if (stCampaignInput) stCampaignInput.value = "";
    const stMatchType = document.getElementById("search-terms-match-type");
    if (stMatchType) stMatchType.value = "";
    if (stWasteFilter) stWasteFilter.value = "";
    const stMinSpend = document.getElementById("search-terms-min-spend");
    if (stMinSpend) stMinSpend.value = "";
    _updateSearchTermsFilterNote();
    loadSearchTerms({ reset: true });
  });
  if (stRefreshBtn)  stRefreshBtn.addEventListener("click",  () => loadSearchTerms({ reset: true }));
  if (stLoadMoreBtn) stLoadMoreBtn.addEventListener("click", () => loadSearchTerms({ reset: false, cursor: searchTermsNextCursor }));

  if (stWasteFilter) stWasteFilter.addEventListener("change", () => {
    _updateSearchTermsFilterNote();
    // Always refetch — backend applies waste_state filter server-side.
    loadSearchTerms({ reset: true });
  });
  if (stExportBtn) stExportBtn.addEventListener("click", downloadSearchTermsCSV);

  // Enter key in query/campaign inputs triggers apply
  [stQueryInput, stCampaignInput].forEach((el) => {
    if (el) el.addEventListener("keydown", (e) => {
      if (e.key === "Enter") loadSearchTerms({ reset: true });
    });
  });

  // Wire up n-gram controls
  const ngramRefreshBtn = document.getElementById("ngrams-refresh-btn");
  const ngramApplyBtn   = document.getElementById("ngrams-apply-btn");
  const ngramClearBtn   = document.getElementById("ngrams-clear-btn");
  const ngramExportBtn  = document.getElementById("ngrams-export-csv-btn");

  if (ngramRefreshBtn) ngramRefreshBtn.addEventListener("click", loadNgrams);
  if (ngramApplyBtn)   ngramApplyBtn.addEventListener("click",   loadNgrams);
  if (ngramClearBtn)   ngramClearBtn.addEventListener("click",   clearNgramFilters);
  if (ngramExportBtn)  ngramExportBtn.addEventListener("click",  downloadNgramsCSV);

  ["ngrams-query", "ngrams-campaign", "ngrams-min-spend"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("keydown", (e) => {
      if (e.key === "Enter") loadNgrams();
    });
  });

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

  // Check auth and load initial page
  const isAuth = await checkAuth();
  if (isAuth) {
    navigate("dashboard");
    // Load thresholds in background — do not block first page render.
    loadUiThresholds().then(() => {
      if (_currentPage) loadPage(_currentPage);
    }).catch((err) => {
      console.warn("[loadUiThresholds] background load failed; continuing with defaults.", err);
    });
  }
});
