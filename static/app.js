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

const PAGES = ["dashboard", "campaigns", "waste", "geo", "keywords", "leads", "deals", "opportunities", "scheduler", "health"];

// Junk rate thresholds (from config/thresholds.yaml doctrine)
const JUNK_RATE_LOW_THRESHOLD  = 15;  // below this → green
const JUNK_RATE_HIGH_THRESHOLD = 30;  // above this → red

// Deal pipeline stages (Phase 1 read-only reference)
const DEAL_PIPELINE_STAGES = ["Proposal", "Trials", "Pricing Acceptance", "Invoice Sent", "Won"];

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

// ── Auth flow ──────────────────────────────────────────────────────────────

function showLogin() {
  document.getElementById("login-screen").style.display = "flex";
  document.getElementById("app").style.display = "none";
  _currentUser = null;
}

function showApp(user) {
  document.getElementById("login-screen").style.display = "none";
  document.getElementById("app").style.display = "flex";
  _currentUser = user;
  applySidebarUser(user);
  // Show/hide System Health nav item
  const healthNav = document.getElementById("nav-health-item");
  if (healthNav) healthNav.hidden = user.role !== "admin";
  // Start with sidebar health check and data freshness
  loadSidebarHealth();
  loadDataFreshness();
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
    case "dashboard":     loadDashboard();     break;
    case "campaigns":     loadCampaigns();     break;
    case "waste":         loadWaste();         break;
    case "geo":           loadGeo();           break;
    case "keywords":      loadKeywords();      break;
    case "leads":         loadLeads();         break;
    case "deals":         loadDeals();         break;
    case "opportunities": loadOpportunities(); break;
    case "scheduler":     loadScheduler();     break;
    case "health":        loadHealth();        break;
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
  } else if (ageDays > 2) {
    statusEl.textContent = `Latest recorded run is stale · ${dateStr} · ${runType} · ${status}`;
    statusEl.className   = "freshness-status freshness-warning";
  } else {
    statusEl.textContent = `Latest recorded run · ${dateStr} · ${runType} · ${status}`;
    statusEl.className   = "freshness-status freshness-ok";
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

  // Load campaign data for the verdict summary panel and alerts panel
  try {
    const campaigns = await fetchJSON(`/api/campaigns?days=${days}`);
    renderVerdictSummary(campaigns.campaigns || []);
    renderAlerts(campaigns.campaigns || []);
  } catch (_) {
    renderVerdictSummaryEmpty();
    renderAlertsEmpty();
  }
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
          <th></th>
        </tr>
      </thead>`;

    const tbody = sorted.map((c) => {
      const v       = (c.latest_verdict || "").toUpperCase();
      const junkPct = c.avg_junk_rate_pct;
      const junkCls = junkPct == null ? "" :
                      junkPct < JUNK_RATE_LOW_THRESHOLD   ? "junk--low" :
                      junkPct <= JUNK_RATE_HIGH_THRESHOLD ? "junk--mid" : "junk--high";
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
      const barCls  = junkPct < JUNK_RATE_LOW_THRESHOLD   ? "progress-bar__fill--low" :
                      junkPct <= JUNK_RATE_HIGH_THRESHOLD ? "progress-bar__fill--mid" : "progress-bar__fill--high";
      const junkCls = junkPct < JUNK_RATE_LOW_THRESHOLD   ? "junk--low" :
                      junkPct <= JUNK_RATE_HIGH_THRESHOLD ? "junk--mid" : "junk--high";
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
  if (junkPct < JUNK_RATE_LOW_THRESHOLD) {
    return `<span class="junk-rate-badge junk-rate-badge--low">${junkPct.toFixed(1)}%</span>`;
  }
  if (junkPct <= JUNK_RATE_HIGH_THRESHOLD) {
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
  const highJunk        = merged.filter((r) => r.junk_rate_pct !== null && r.junk_rate_pct >= JUNK_RATE_HIGH_THRESHOLD).length;

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
  if (n >= 8)      cls = "quality-score-strong";
  else if (n >= 5) cls = "quality-score-medium";
  else             cls = "quality-score-weak";
  return `<span class="quality-score-badge ${cls}">${n.toFixed(1)}</span>`;
}

async function loadKeywords() {
  const tableEl   = document.getElementById("kw-table-body");
  const summaryEl = document.getElementById("kw-matchtype-summary");

  if (tableEl)   tableEl.innerHTML   = `<p class="empty-state" style="padding:var(--space-5)">Loading keywords…</p>`;
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

// Threshold above which a keyword row gets subtle high-spend emphasis (matches waste page convention)
const KW_HIGH_SPEND_USD = 100;

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
    const highSpend = (r.spend_usd || 0) >= KW_HIGH_SPEND_USD;
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

// ── System Health page ─────────────────────────────────────────────────────

async function loadHealth() {
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
}

// ── Campaign Detail Drawer ─────────────────────────────────────────────────

let _drawerOpenCampaign = null;  // campaign name string currently shown in drawer, or null

async function openCampaignDrawer(campaignName) {
  if (!campaignName) return;
  _drawerOpenCampaign = campaignName;

  const overlay   = document.getElementById("campaign-drawer-overlay");
  const drawer    = document.getElementById("campaign-drawer");
  const titleEl   = document.getElementById("drawer-campaign-title");
  const bodyEl    = document.getElementById("campaign-drawer-body");

  // Show overlay + drawer immediately with loading state
  if (overlay) { overlay.hidden = false; overlay.removeAttribute("aria-hidden"); }
  if (drawer)  { drawer.hidden  = false; }
  if (titleEl) titleEl.textContent = campaignName;
  if (bodyEl)  bodyEl.innerHTML = `<p class="empty-state"><span class="spinner"></span> Loading campaign detail…</p>`;

  // Trap focus — bind Escape key once
  document.addEventListener("keydown", _drawerEscapeHandler);

  try {
    const data = await fetchJSON(
      `/api/campaigns/${encodeURIComponent(campaignName)}/detail?days=${getSelectedDays()}`
    );
    // Guard stale response: drawer may have been closed or switched while fetching
    if (_drawerOpenCampaign !== campaignName) return;
    renderCampaignDrawer(data);
  } catch (err) {
    if (_drawerOpenCampaign !== campaignName) return;
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
  const overlay = document.getElementById("campaign-drawer-overlay");
  const drawer  = document.getElementById("campaign-drawer");
  if (overlay) { overlay.hidden = true; overlay.setAttribute("aria-hidden", "true"); }
  if (drawer)  drawer.hidden = true;
  document.removeEventListener("keydown", _drawerEscapeHandler);
}

function _drawerEscapeHandler(e) {
  if (e.key === "Escape") closeCampaignDrawer();
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
    bodyEl.innerHTML = `
      <div class="drawer-section">
        <p class="drawer-empty">No campaign detail available for this window.</p>
      </div>`;
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
                      lq.junk_rate_pct < JUNK_RATE_LOW_THRESHOLD   ? "junk--low" :
                      lq.junk_rate_pct <= JUNK_RATE_HIGH_THRESHOLD ? "junk--mid" : "junk--high";
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
        <p class="drawer-source-note">Unknown contacts are excluded from junk-rate denominator. Total in window: ${lq.total_leads}.</p>
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
                      r.junk_rate_pct < JUNK_RATE_LOW_THRESHOLD   ? "junk--low" :
                      r.junk_rate_pct <= JUNK_RATE_HIGH_THRESHOLD ? "junk--mid" : "junk--high";
      return `
        <tr>
          <td class="td--name">${escapeHtml(r.country)}</td>
          <td>${r.total_leads}</td>
          <td>${r.confirmed_sqls}</td>
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
            <tr><th>Country</th><th>Leads</th><th>SQLs</th><th>Junk</th><th>Wrong Fit</th><th>Unknown</th><th>Junk Rate</th></tr>
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
          <p class="drawer-source-note" style="margin-top:var(--space-2)">Review-only — no push action.</p>
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

  bodyEl.innerHTML = headerHtml + kpiHtml + lqHtml + countryHtml + kwHtml + wasteHtml + leadsHtml + footerHtml;

  // Wire up waste copy button if present
  const wcBtn = document.getElementById("drawer-waste-copy-btn");
  if (wcBtn) {
    wcBtn.addEventListener("click", () => copyDrawerWasteTerms(wasteTerms, wcBtn));
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
  if (wasteSearch)  wasteSearch.addEventListener("input",  applyWasteFilters);
  if (wasteCatSel)  wasteCatSel.addEventListener("change", applyWasteFilters);
  if (wasteCampSel) wasteCampSel.addEventListener("change", applyWasteFilters);
  if (wasteCopyBtn) wasteCopyBtn.addEventListener("click", copyWasteTerms);

  // Wire up geo search
  const geoSearch = document.getElementById("geo-search");
  if (geoSearch) geoSearch.addEventListener("input", applyGeoSearch);

  // Wire up keywords filter controls
  const kwSearch   = document.getElementById("kw-filter-search");
  const kwCampSel  = document.getElementById("kw-filter-campaign");
  const kwMatchSel = document.getElementById("kw-filter-matchtype");
  const kwCopyBtn  = document.getElementById("kw-copy-btn");
  if (kwSearch)   kwSearch.addEventListener("input",   applyKeywordFilters);
  if (kwCampSel)  kwCampSel.addEventListener("change", applyKeywordFilters);
  if (kwMatchSel) kwMatchSel.addEventListener("change", applyKeywordFilters);
  if (kwCopyBtn)  kwCopyBtn.addEventListener("click",  copyKeywordRows);

  // Wire up campaign drawer close controls
  const drawerCloseBtn = document.getElementById("drawer-close-btn");
  const drawerOverlay  = document.getElementById("campaign-drawer-overlay");
  if (drawerCloseBtn) drawerCloseBtn.addEventListener("click", closeCampaignDrawer);
  if (drawerOverlay)  drawerOverlay.addEventListener("click", closeCampaignDrawer);

  // Check auth and load initial page
  const isAuth = await checkAuth();
  if (isAuth) {
    navigate("dashboard");
  }
});
