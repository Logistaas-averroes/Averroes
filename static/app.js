/**
 * static/app.js
 *
 * Logistaas Ads Intelligence — 5-page SPA frontend logic.
 * PR-ADS-023 — Brand-Aligned Dashboard Rebuild
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

// ── Session state ──────────────────────────────────────────────────────────

let _currentUser = null;   // { username, role } or null
let _currentPage = null;   // active page id string
let _reportCache = null;   // { text, sections } or null — cache per session

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

function parseDollar(str) {
  if (!str || str === "N/A" || str === "—") return null;
  const m = String(str).replace(/[$,]/g, "").match(/[\d.]+/);
  return m ? parseFloat(m[0]) : null;
}

function parseNum(str) {
  if (!str || str === "N/A" || str === "—") return null;
  const m = String(str).replace(/,/g, "").match(/\d+/);
  return m ? parseInt(m[0], 10) : null;
}

function parsePct(str) {
  if (!str || str === "N/A" || str === "—") return null;
  const m = String(str).match(/([\d.]+)%/);
  return m ? parseFloat(m[1]) : null;
}

function fmtDollar(n) {
  if (n === null || n === undefined) return "—";
  if (n >= 1000) return "$" + (n / 1000).toFixed(1) + "k";
  return "$" + n.toFixed(0);
}

// Strip emojis and extra whitespace from a string (e.g. "✅ SCALE" → "SCALE")
function stripEmoji(str) {
  return str
    .replace(/[\u{1F000}-\u{1FFFF}\u{2600}-\u{27FF}\u{FE00}-\u{FEFF}]/gu, "")
    .replace(/[\u{200D}]/gu, "")
    .trim();
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

// ── Markdown report parser ─────────────────────────────────────────────────

/**
 * Parse a markdown report string into a map of section name → table lines.
 * Section names are normalised (lowercase, trimmed, leading number stripped).
 * Wrapped in try/catch — returns empty object on any failure.
 */
function parseMarkdownReport(md) {
  try {
    const lines = md.split("\n");
    const sections = {};
    let currentKey = null;
    let tableLines = [];

    for (const line of lines) {
      if (line.startsWith("## ")) {
        if (currentKey !== null) {
          sections[currentKey] = tableLines;
        }
        // Normalise: strip leading "N. " or "N." number prefix, lowercase
        const title = line.replace(/^##\s+/, "").replace(/^\d+\.\s*/, "").trim().toLowerCase();
        currentKey = title;
        tableLines = [];
      } else if (currentKey !== null && line.trimStart().startsWith("|")) {
        tableLines.push(line);
      }
    }
    if (currentKey !== null) {
      sections[currentKey] = tableLines;
    }
    return sections;
  } catch (_) {
    return {};
  }
}

/**
 * Parse markdown table lines into { headers, rows }.
 * Filters out separator rows (|---|---|).
 * Returns { headers: string[], rows: string[][] }.
 */
function parseMdTable(tableLines) {
  try {
    const isSeparator = (line) => /^\|[\s\-:|]+\|$/.test(line.trim());
    const dataLines = tableLines.filter((l) => !isSeparator(l));
    if (dataLines.length === 0) return { headers: [], rows: [] };

    const parseRow = (line) =>
      line.split("|").slice(1, -1).map((c) => c.trim());

    const headers = parseRow(dataLines[0]);
    const rows = dataLines.slice(1).map(parseRow);
    return { headers, rows };
  } catch (_) {
    return { headers: [], rows: [] };
  }
}

/** Get or fetch the parsed report. Caches result for the session. */
async function getReport() {
  if (_reportCache) return _reportCache;
  try {
    const res = await fetch("/reports/latest/raw", { credentials: "same-origin" });
    if (!res.ok) return { text: null, sections: {} };
    const text = await res.text();
    const sections = parseMarkdownReport(text);
    _reportCache = { text, sections };
    return _reportCache;
  } catch (_) {
    return { text: null, sections: {} };
  }
}

/** Find a section by matching key fragment (case-insensitive substring). */
function findSection(sections, fragment) {
  const frag = fragment.toLowerCase();
  const key = Object.keys(sections).find((k) => k.includes(frag));
  return key ? sections[key] : [];
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
  document.getElementById("login-screen").hidden = false;
  document.getElementById("app").hidden = true;
  _currentUser = null;
  _reportCache = null;
}

function showApp(user) {
  document.getElementById("login-screen").hidden = true;
  document.getElementById("app").hidden = false;
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
  _reportCache = null;
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
  // Load KPI + verdict summary from report in parallel with run data
  const [, latestRun] = await Promise.allSettled([
    loadDashboardReport(),
    fetchJSON("/runs/latest"),
  ]);

  renderRunTimeline(latestRun.status === "fulfilled" ? latestRun.value : null);
}

async function loadDashboardReport() {
  const { sections } = await getReport();

  // Campaign Truth Table → KPIs + verdict summary + alerts
  const campLines = findSection(sections, "campaign truth table");
  const { rows: campRows } = parseMdTable(campLines);

  if (campRows.length === 0) {
    renderKPIsEmpty();
    renderVerdictSummaryEmpty();
    renderAlertsEmpty();
    return;
  }

  // Column indices (best-effort — fall back gracefully)
  // Headers: Campaign | Spend (30d) | Leads | Confirmed SQLs | Junk Rate | CPQL | Verdict | Reason
  let totalSpend = 0;
  let totalSQLs  = 0;
  let confirmedWaste = null;
  const verdicts = [];

  for (const row of campRows) {
    if (row.length < 7) continue;
    const spend = parseDollar(row[1]);
    const sqls  = parseNum(row[3]);
    const verdict = stripEmoji(row[6] || "");
    if (spend !== null) totalSpend += spend;
    if (sqls !== null)  totalSQLs  += sqls;
    if (verdict) verdicts.push({ name: row[0], spend, sqls, verdict });
  }

  // Waste from "## 4. Waste Detection Summary"
  const wasteLines = findSection(sections, "waste detection");
  if (wasteLines.length > 0) {
    const { rows: wasteRows } = parseMdTable(wasteLines);
    for (const r of wasteRows) {
      if (r.length >= 2 && r[0].toLowerCase().includes("confirmed waste")) {
        confirmedWaste = parseDollar(r[1]);
        break;
      }
    }
  }

  renderKPIs(totalSpend, totalSQLs, confirmedWaste);
  renderVerdictSummary(verdicts);
  renderAlerts(verdicts);
}

function renderKPIs(spend, sqls, waste) {
  const spendEl  = document.getElementById("kpi-spend");
  const sqlsEl   = document.getElementById("kpi-sqls");
  const cpqlEl   = document.getElementById("kpi-cpql");
  const wasteEl  = document.getElementById("kpi-waste");

  if (spendEl)  spendEl.textContent  = spend > 0 ? fmtDollar(spend) : "—";
  if (sqlsEl)   sqlsEl.textContent   = sqls > 0  ? String(sqls)     : "0";
  if (cpqlEl)   cpqlEl.textContent   = (sqls > 0 && spend > 0) ? fmtDollar(spend / sqls) : "N/A";
  if (wasteEl)  wasteEl.textContent  = waste !== null ? fmtDollar(waste) : "—";
}

function renderKPIsEmpty() {
  ["kpi-spend", "kpi-sqls", "kpi-cpql", "kpi-waste"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.textContent = "—";
  });
}

function renderVerdictSummary(verdicts) {
  const el = document.getElementById("dash-verdict-body");
  if (!el) return;

  if (verdicts.length === 0) {
    el.innerHTML = `<p class="empty-state">No campaign data yet. Trigger a weekly run to populate.</p>`;
    return;
  }

  // Sort by spend desc
  const sorted = [...verdicts].sort((a, b) => (b.spend || 0) - (a.spend || 0));
  const maxSpend = sorted[0].spend || 1;

  const rows = sorted.map((c) => {
    const pct   = Math.max(5, Math.round(((c.spend || 0) / maxSpend) * 100));
    const v     = c.verdict ? c.verdict.toUpperCase() : "";
    const spend = c.spend !== null ? fmtDollar(c.spend) : "—";
    return `
      <div class="verdict-row">
        <div class="verdict-row__name" title="${escapeHtml(c.name)}">${escapeHtml(c.name)}</div>
        <div class="verdict-row__bar">
          <div class="verdict-row__bar-fill verdict-row__bar-fill--${escapeHtml(v)}" style="width:${pct}%"></div>
        </div>
        <div class="verdict-row__meta">
          <span class="verdict-row__spend">${spend}</span>
          ${verdictBadge(v)}
        </div>
      </div>`;
  }).join("");

  el.innerHTML = rows;
}

function renderVerdictSummaryEmpty() {
  const el = document.getElementById("dash-verdict-body");
  if (el) el.innerHTML = `<p class="empty-state">No campaign data yet. Trigger a weekly run to populate.</p>`;
}

function renderAlerts(verdicts) {
  const el = document.getElementById("dash-alerts-body");
  if (!el) return;

  const alerts = verdicts.filter((c) => c.verdict === "FIX" || c.verdict === "CUT");

  if (alerts.length === 0) {
    el.innerHTML = `<p class="empty-state">No active alerts. All campaigns are within acceptable parameters.</p>`;
    return;
  }

  const icon = (v) => v === "CUT"
    ? `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" class="alert-icon alert-icon--cut"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`
    : `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" class="alert-icon alert-icon--fix"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`;

  el.innerHTML = alerts.map((c) => `
    <div class="alert-item">
      ${icon(c.verdict)}
      <div class="alert-text">
        Campaign <span class="alert-campaign">${escapeHtml(c.name)}</span>
        — verdict ${verdictBadge(c.verdict)}
        ${c.spend !== null ? `· Spend: ${fmtDollar(c.spend)}` : ""}
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
  const tableEl   = document.getElementById("camp-table-body");
  const scaleEl   = document.getElementById("vc-scale");
  const fixEl     = document.getElementById("vc-fix");
  const holdEl    = document.getElementById("vc-hold");
  const cutEl     = document.getElementById("vc-cut");

  if (tableEl) tableEl.innerHTML = `<p class="empty-state" style="padding:var(--space-5)">Loading campaigns…</p>`;

  try {
    const { sections } = await getReport();
    const campLines = findSection(sections, "campaign truth table");
    const { headers, rows } = parseMdTable(campLines);

    if (rows.length === 0) {
      if (tableEl) tableEl.innerHTML = `<p class="empty-state" style="padding:var(--space-5)">No campaign data yet. Trigger a weekly run.</p>`;
      ["vc-scale", "vc-fix", "vc-hold", "vc-cut"].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.textContent = "0";
      });
      return;
    }

    // Count verdicts (column 6)
    let nScale = 0, nFix = 0, nHold = 0, nCut = 0;
    const dataRows = rows.map((row) => {
      const v = row.length >= 7 ? stripEmoji(row[6] || "") : "";
      if (v === "SCALE") nScale++;
      else if (v === "FIX")   nFix++;
      else if (v === "HOLD")  nHold++;
      else if (v === "CUT")   nCut++;
      return { row, verdict: v };
    });

    if (scaleEl) scaleEl.textContent = String(nScale);
    if (fixEl)   fixEl.textContent   = String(nFix);
    if (holdEl)  holdEl.textContent  = String(nHold);
    if (cutEl)   cutEl.textContent   = String(nCut);

    // Sort by spend descending (column 1)
    dataRows.sort((a, b) => (parseDollar(b.row[1]) || 0) - (parseDollar(a.row[1]) || 0));

    // Render table
    // Headers: Campaign | Spend (30d) | Leads | Confirmed SQLs | Junk Rate | CPQL | Verdict | Reason
    const thead = `
      <thead>
        <tr>
          <th>Campaign</th>
          <th>Spend (30d)</th>
          <th class="td--num">Leads</th>
          <th class="td--num">SQLs</th>
          <th>Junk %</th>
          <th class="td--num">CPQL</th>
          <th>Verdict</th>
          <th>Reason</th>
        </tr>
      </thead>`;

    const tbody = dataRows.map(({ row, verdict }) => {
      const junkPct  = parsePct(row[4] || "");
      const junkCls  = junkPct === null ? "" : junkPct < 15 ? "junk--low" : junkPct <= 30 ? "junk--mid" : "junk--high";
      const sqls     = parseNum(row[3] || "");
      // CPQL shows N/A when SQLs = 0
      const cpql     = sqls === 0 ? "N/A" : fmt(row[5] || "N/A");

      return `
        <tr>
          <td class="td--name">${escapeHtml(row[0] || "—")}</td>
          <td class="td--num">${escapeHtml(row[1] || "—")}</td>
          <td class="td--num">${escapeHtml(row[2] || "—")}</td>
          <td class="td--num">${escapeHtml(row[3] || "—")}</td>
          <td class="${junkCls}">${escapeHtml(row[4] || "—")}</td>
          <td class="td--num ${cpql === "N/A" ? "td--na" : ""}">${cpql}</td>
          <td>${verdictBadge(verdict)}</td>
          <td>${escapeHtml(row[7] || "—")}</td>
        </tr>`;
    }).join("");

    if (tableEl) tableEl.innerHTML = `<table class="data-table">${thead}<tbody>${tbody}</tbody></table>`;
  } catch (_) {
    if (tableEl) tableEl.innerHTML = `<p class="empty-state" style="padding:var(--space-5)">Could not load campaign data.</p>`;
  }
}

// ── Lead Quality page ──────────────────────────────────────────────────────

async function loadLeads() {
  const tableEl    = document.getElementById("leads-table-body");
  const totalEl    = document.getElementById("leads-total");
  const sqlsEl     = document.getElementById("leads-sqls");
  const junkEl     = document.getElementById("leads-junk");
  const progressEl = document.getElementById("leads-progress");

  if (tableEl) tableEl.innerHTML = `<p class="empty-state" style="padding:var(--space-5)">Loading lead quality data…</p>`;

  try {
    const { sections } = await getReport();
    const leadLines = findSection(sections, "lead quality breakdown");
    const { rows } = parseMdTable(leadLines);

    if (rows.length === 0) {
      if (tableEl) tableEl.innerHTML = `<p class="empty-state" style="padding:var(--space-5)">No lead quality data yet. Trigger a weekly run.</p>`;
      [totalEl, sqlsEl, junkEl, progressEl].forEach((el) => { if (el) el.textContent = "—"; });
      return;
    }

    // Headers: Campaign | Total | Qualified (SQL) | In Progress | Junk | Wrong Fit | No Status | Junk Rate
    let sumTotal = 0, sumSQL = 0, sumJunk = 0, sumProgress = 0;

    rows.forEach((row) => {
      sumTotal    += parseNum(row[1] || "") || 0;
      sumSQL      += parseNum(row[2] || "") || 0;
      sumProgress += parseNum(row[3] || "") || 0;
      sumJunk     += parseNum(row[4] || "") || 0;
    });

    if (totalEl)    totalEl.textContent    = sumTotal    > 0 ? String(sumTotal)    : "0";
    if (sqlsEl)     sqlsEl.textContent     = sumSQL      > 0 ? String(sumSQL)      : "0";
    if (junkEl)     junkEl.textContent     = sumJunk     > 0 ? String(sumJunk)     : "0";
    if (progressEl) progressEl.textContent = sumProgress > 0 ? String(sumProgress) : "0";

    const thead = `
      <thead>
        <tr>
          <th>Campaign</th>
          <th class="td--num">Total</th>
          <th class="td--num">SQL</th>
          <th class="td--num">In Progress</th>
          <th class="td--num">Junk</th>
          <th class="td--num">Wrong Fit</th>
          <th class="td--num">No Status</th>
          <th>Junk Rate</th>
        </tr>
      </thead>`;

    const tbody = rows.map((row) => {
      const junkPct  = parsePct(row[7] || "");
      const pct      = junkPct !== null ? Math.min(100, Math.round(junkPct)) : 0;
      const barCls   = pct < 15 ? "progress-bar__fill--low" : pct <= 30 ? "progress-bar__fill--mid" : "progress-bar__fill--high";
      const junkCls  = pct < 15 ? "junk--low" : pct <= 30 ? "junk--mid" : "junk--high";

      return `
        <tr>
          <td class="td--name">${escapeHtml(row[0] || "—")}</td>
          <td class="td--num">${escapeHtml(row[1] || "—")}</td>
          <td class="td--num">${escapeHtml(row[2] || "—")}</td>
          <td class="td--num">${escapeHtml(row[3] || "—")}</td>
          <td class="td--num">${escapeHtml(row[4] || "—")}</td>
          <td class="td--num">${escapeHtml(row[5] || "—")}</td>
          <td class="td--num">${escapeHtml(row[6] || "—")}</td>
          <td>
            <div style="display:flex;align-items:center;gap:8px;">
              <div class="progress-bar" style="width:80px">
                <div class="progress-bar__fill ${barCls}" style="width:${pct}%"></div>
              </div>
              <span class="${junkCls}" style="font-size:12px;font-weight:500;">${escapeHtml(row[7] || "—")}</span>
            </div>
          </td>
        </tr>`;
    }).join("");

    if (tableEl) tableEl.innerHTML = `<table class="data-table">${thead}<tbody>${tbody}</tbody></table>`;
  } catch (_) {
    if (tableEl) tableEl.innerHTML = `<p class="empty-state" style="padding:var(--space-5)">Could not load lead quality data.</p>`;
  }
}

// ── Deals page ─────────────────────────────────────────────────────────────

async function loadDeals() {
  const funnelEl = document.getElementById("deals-funnel-body");
  const tableEl  = document.getElementById("deals-table-body");

  const EMPTY_MSG = "No GCLID-matched deals found yet. Run the weekly report to populate.";

  if (funnelEl) funnelEl.innerHTML = `<p class="empty-state">${EMPTY_MSG}</p>`;
  if (tableEl)  tableEl.innerHTML  = `<p class="empty-state" style="padding:var(--space-5)">${EMPTY_MSG}</p>`;

  try {
    const { sections } = await getReport();

    // Look for a Deals section in the report (may not exist in Phase 1 reports)
    const dealLines = findSection(sections, "deal");
    const { rows: dealRows } = parseMdTable(dealLines);

    if (dealRows.length === 0) {
      return; // Empty state already set
    }

    // Attempt to render pipeline stages from deal rows
    // Expected columns (best-effort): Company | Country | Stage | Amount | Keyword | Campaign
    const stages = ["Proposal", "Trials", "Pricing Acceptance", "Invoice Sent", "Won"];
    const stageCounts = {};
    stages.forEach((s) => { stageCounts[s] = 0; });

    dealRows.forEach((row) => {
      const stage = row[2] || "";
      const matchedStage = stages.find((s) => stage.toLowerCase().includes(s.toLowerCase()));
      if (matchedStage) stageCounts[matchedStage]++;
    });

    const maxCount = Math.max(...Object.values(stageCounts), 1);

    if (funnelEl) {
      funnelEl.innerHTML = `
        <div class="funnel">
          ${stages.map((s) => {
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
          <th>Keyword</th>
          <th>Campaign</th>
        </tr>
      </thead>`;

    const tbody = dealRows.map((row) => {
      const isWon = (row[2] || "").toLowerCase().includes("won");
      return `
        <tr${isWon ? ' class="row--won"' : ""}>
          <td class="td--name">${escapeHtml(row[0] || "—")}</td>
          <td>${escapeHtml(row[1] || "—")}</td>
          <td>${escapeHtml(row[2] || "—")}</td>
          <td class="td--num">${escapeHtml(row[3] || "—")}</td>
          <td>${escapeHtml(row[4] || "—")}</td>
          <td>${escapeHtml(row[5] || "—")}</td>
        </tr>`;
    }).join("");

    if (tableEl) tableEl.innerHTML = `<table class="data-table">${thead}<tbody>${tbody}</tbody></table>`;
  } catch (_) {
    if (funnelEl) funnelEl.innerHTML = `<p class="empty-state">${EMPTY_MSG}</p>`;
  }
}

// ── Opportunities page ─────────────────────────────────────────────────────

async function loadOpportunities() {
  const el = document.getElementById("opps-body");
  if (!el) return;

  el.innerHTML = `<p class="empty-state">Loading opportunities…</p>`;

  try {
    const { sections } = await getReport();

    // Look for opportunities / in-progress section
    const oppLines = findSection(sections, "opportunit");
    const leadLines = findSection(sections, "lead quality breakdown");

    // Parse lead quality data and filter for "In Progress" status
    const { rows: leadRows } = parseMdTable(leadLines);

    // Build opportunity list from leads in progress (col 3 > 0)
    const inProgress = leadRows.filter((row) => (parseNum(row[3] || "") || 0) > 0);

    // Also try dedicated opportunities section if it exists
    const { rows: oppRows } = parseMdTable(oppLines);
    const allOpps = oppRows.length > 0 ? oppRows : [];

    if (allOpps.length === 0 && inProgress.length === 0) {
      el.innerHTML = `<p class="empty-state">No active opportunities found in latest report.</p>`;
      return;
    }

    if (allOpps.length > 0) {
      // Render from dedicated section: Company | Country | Keyword | Campaign | Status | Created
      // Group by status (Meeting Booked first)
      const booked  = allOpps.filter((r) => (r[4] || "").toLowerCase().includes("meeting booked"));
      const pending = allOpps.filter((r) => !booked.includes(r));

      el.innerHTML = renderOppGroup("Meeting Booked", booked)
                   + renderOppGroup("Pending Meeting", pending);
    } else {
      // Fall back: show campaigns with in-progress leads as summary cards
      el.innerHTML = `
        <p class="opp-group-title">Campaigns with In-Progress Leads</p>
        <div class="opp-grid">
          ${inProgress.map((row) => `
            <div class="opp-card">
              <div class="opp-card__company">${escapeHtml(row[0] || "Unknown")}</div>
              <div class="opp-card__meta">
                <span class="opp-card__tag">In Progress: ${escapeHtml(row[3] || "0")}</span>
                <span class="opp-card__tag">Total: ${escapeHtml(row[1] || "0")}</span>
              </div>
            </div>`).join("")}
        </div>`;
    }
  } catch (_) {
    el.innerHTML = `<p class="empty-state">Could not load opportunity data.</p>`;
  }
}

function renderOppGroup(title, rows) {
  if (rows.length === 0) return "";
  return `
    <p class="opp-group-title">${escapeHtml(title)}</p>
    <div class="opp-grid">
      ${rows.map((row) => `
        <div class="opp-card">
          <div class="opp-card__company">${escapeHtml(row[0] || "Unknown")}</div>
          <div class="opp-card__meta">
            ${row[1] ? `<span class="opp-card__tag">${escapeHtml(row[1])}</span>` : ""}
            ${row[2] ? `<span class="opp-card__tag">${escapeHtml(row[2])}</span>` : ""}
            ${row[3] ? `<span class="opp-card__tag">${escapeHtml(row[3])}</span>` : ""}
          </div>
          ${row[5] ? `<div style="font-size:11px;color:var(--text-muted)">Created: ${escapeHtml(row[5])}</div>` : ""}
        </div>`).join("")}
    </div>`;
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
      _reportCache = null; // Invalidate cache
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

  // Check auth and load initial page
  const isAuth = await checkAuth();
  if (isAuth) {
    navigate("dashboard");
  }
});
