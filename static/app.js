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

const PAGES = ["dashboard", "campaigns", "leads", "deals", "opportunities", "scheduler", "health"];

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
  // Reload current page with new window
  if (_currentPage) loadPage(_currentPage);
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
  // Start with sidebar health check
  loadSidebarHealth();
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

// ── Dashboard page ─────────────────────────────────────────────────────────

async function loadDashboard() {
  const days = getSelectedDays();

  const [summaryResult, runResult] = await Promise.allSettled([
    fetchJSON(`/api/summary?days=${days}`),
    fetchJSON("/runs/latest"),
  ]);

  const summary = summaryResult.status === "fulfilled" ? summaryResult.value : null;
  const run     = runResult.status === "fulfilled"     ? runResult.value     : null;

  renderKPIs(summary);
  renderRunTimeline(run);

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

function renderRunTimeline(runData) {
  const el = document.getElementById("dash-run-body");
  if (!el) return;

  if (!runData || runData.status === "empty" || !runData.run_type) {
    el.innerHTML = `<p class="empty-state">No run history yet. Trigger a manual run or wait for the next scheduled run.</p>`;
    return;
  }

  const dotCls = runData.status === "success" ? "run-entry__dot--success"
               : runData.status === "failed"  ? "run-entry__dot--failed"
               : "run-entry__dot--empty";

  const outcome = runData.error_message
    ? `Error: ${escapeHtml(runData.error_message)}`
    : runData.delivery_success === false
      ? "Delivery failed"
      : runData.status === "success"
        ? "Completed successfully"
        : `Status: ${escapeHtml(runData.status || "unknown")}`;

  el.innerHTML = `
    <div class="run-entry">
      <div class="run-entry__dot ${dotCls}"></div>
      <div class="run-entry__meta">
        <div class="run-entry__type">${fmt(runData.run_type)} run</div>
        <div class="run-entry__time">${fmtDate(runData.finished_at || runData.started_at)}</div>
        <div class="run-entry__outcome">${outcome}</div>
      </div>
      ${statusBadge(runData.status)}
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
          <td class="td--num">—</td>
          <td class="td--num">${c.total_confirmed_sqls != null ? String(c.total_confirmed_sqls) : "0"}</td>
          <td class="${junkCls}">${junkStr}</td>
          <td class="td--num ${cpql === "N/A" ? "td--na" : ""}">${cpql}</td>
          <td>${verdictBadge(v)}</td>
          <td class="td--num">${c.run_count != null ? String(c.run_count) : "—"}</td>
        </tr>`;
    }).join("");

    if (tableEl) tableEl.innerHTML =
      `<table class="data-table">${thead}<tbody>${tbody}</tbody></table>`;

  } catch (_) {
    if (tableEl) tableEl.innerHTML =
      `<p class="empty-state" style="padding:var(--space-5)">Could not load campaign data.</p>`;
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

// ── Opportunities page ─────────────────────────────────────────────────────

async function loadOpportunities() {
  const el = document.getElementById("opps-body");
  if (!el) return;

  el.innerHTML = `<p class="empty-state">Loading opportunities…</p>`;

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

    // Filter to in-progress only
    const inProgress = Array.from(seen.values())
      .filter((l) => l.status_category === "in_progress");

    if (inProgress.length === 0) {
      el.innerHTML = `<p class="empty-state">No active opportunities in the selected window.</p>`;
      return;
    }

    // Group by mql_status
    const booked  = inProgress.filter((l) =>
      (l.mql_status || "").toLowerCase().includes("meeting booked")
    );
    const pending = inProgress.filter((l) =>
      (l.mql_status || "").toLowerCase().includes("pending meeting")
    );
    const other   = inProgress.filter((l) => !booked.includes(l) && !pending.includes(l));

    const renderGroup = (title, leads) => {
      if (leads.length === 0) return "";
      return `
        <p class="opp-group-title">${escapeHtml(title)} (${leads.length})</p>
        <div class="opp-grid">
          ${leads.map((l) => `
            <div class="opp-card">
              <div class="opp-card__company">${escapeHtml(l.mql_status || "In Progress")}</div>
              <div class="opp-card__meta">
                ${l.campaign_name ? `<span class="opp-card__tag">${escapeHtml(l.campaign_name)}</span>` : ""}
                ${l.keyword ? `<span class="opp-card__tag">${escapeHtml(l.keyword)}</span>` : ""}
                ${l.country ? `<span class="opp-card__tag">${escapeHtml(l.country)}</span>` : ""}
              </div>
              <div style="font-size:11px;color:var(--text-muted);margin-top:4px">
                ID: ${escapeHtml(l.contact_id || "—")}
              </div>
            </div>`).join("")}
        </div>`;
    };

    el.innerHTML = renderGroup("Meeting Booked", booked)
                 + renderGroup("Pending Meeting", pending)
                 + renderGroup("Connecting", other);

  } catch (_) {
    el.innerHTML = `<p class="empty-state">Could not load opportunity data.</p>`;
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

  // Check auth and load initial page
  const isAuth = await checkAuth();
  if (isAuth) {
    navigate("dashboard");
  }
});
