/**
 * static/app.js
 *
 * Logistaas Ads Intelligence Dashboard — frontend logic.
 *
 * Rules:
 *  - No hardcoded secrets.
 *  - No external tracking or analytics.
 *  - No third-party JS dependencies.
 *  - Auth state managed via HTTP-only session cookie (not accessible to JS).
 *  - Role-based UI: admin sees run controls; viewer/mdr see read-only dashboard.
 */

"use strict";

// ── Helpers ────────────────────────────────────────────────────────────────

function statusBadge(value, map) {
  const lower = (value || "").toLowerCase();
  const cls = map[lower] || "badge--neutral";
  return `<span class="badge ${cls}"><span class="dot"></span>${escapeHtml(value || "unknown")}</span>`;
}

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

// ── Status badge maps ──────────────────────────────────────────────────────

const STATUS_MAP = {
  ok:      "badge--ok",
  pass:    "badge--pass",
  success: "badge--success",
  fail:    "badge--fail",
  failed:  "badge--failed",
  error:   "badge--error",
  empty:   "badge--empty",
};

// ── Current session state ──────────────────────────────────────────────────

let _currentUser = null;  // { username, role } or null

// ── Fetch wrappers ─────────────────────────────────────────────────────────

async function fetchJSON(url, options) {
  const res = await fetch(url, options);
  if (res.status === 401) {
    showLoginScreen();
    throw new Error("HTTP 401");
  }
  if (res.status === 403) {
    throw new Error("HTTP 403 Forbidden");
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function fetchText(url) {
  const res = await fetch(url);
  if (res.status === 401) {
    showLoginScreen();
    throw new Error("HTTP 401");
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.text();
}

// ── Auth flow ──────────────────────────────────────────────────────────────

function showLoginScreen() {
  const loginEl = document.getElementById("login-screen");
  const dashEl = document.getElementById("dashboard");
  if (loginEl) loginEl.style.display = "flex";
  if (dashEl) dashEl.style.display = "none";
  _currentUser = null;
}

function showDashboard(user) {
  const loginEl = document.getElementById("login-screen");
  const dashEl = document.getElementById("dashboard");
  if (loginEl) loginEl.style.display = "none";
  if (dashEl) dashEl.style.display = "block";
  _currentUser = user;
  applyRoleUI(user);
}

function applyRoleUI(user) {
  // User badge in header
  const userBadge = document.getElementById("user-badge");
  const userName = document.getElementById("user-name");
  const userRoleBadge = document.getElementById("user-role-badge");
  const logoutBtn = document.getElementById("btn-logout");

  if (userBadge) userBadge.style.display = "inline-flex";
  if (userName) userName.textContent = user.username;
  if (userRoleBadge) {
    userRoleBadge.textContent = user.role;
    userRoleBadge.className = `role-badge role-badge--${user.role}`;
  }
  if (logoutBtn) logoutBtn.style.display = "inline-flex";

  // Show run controls only for admin
  const runSection = document.getElementById("run-controls-section");
  if (runSection) {
    runSection.style.display = user.role === "admin" ? "block" : "none";
  }
}

async function checkAuth() {
  try {
    const data = await fetch("/auth/me");
    if (data.status === 401) {
      showLoginScreen();
      return false;
    }
    if (!data.ok) {
      showLoginScreen();
      return false;
    }
    const user = await data.json();
    showDashboard(user);
    return true;
  } catch (e) {
    showLoginScreen();
    return false;
  }
}

async function handleLogin(e) {
  e.preventDefault();
  const usernameEl = document.getElementById("login-username");
  const passwordEl = document.getElementById("login-password");
  const errorEl = document.getElementById("login-error");
  const submitBtn = document.getElementById("login-submit-btn");

  const username = usernameEl ? usernameEl.value.trim() : "";
  const password = passwordEl ? passwordEl.value : "";

  if (!username || !password) {
    if (errorEl) {
      errorEl.textContent = "Username and password are required.";
      errorEl.style.display = "block";
    }
    return;
  }

  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.textContent = "Signing in…";
  }
  if (errorEl) errorEl.style.display = "none";

  try {
    const res = await fetch("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });

    if (res.ok) {
      const user = await res.json();
      // Clear password field for security
      if (passwordEl) passwordEl.value = "";
      showDashboard(user);
      // Load dashboard data
      loadAll();
    } else {
      const body = await res.json().catch(() => ({}));
      if (errorEl) {
        errorEl.textContent = body.detail || "Invalid username or password.";
        errorEl.style.display = "block";
      }
    }
  } catch (err) {
    if (errorEl) {
      errorEl.textContent = "Login failed — please try again.";
      errorEl.style.display = "block";
    }
  } finally {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.textContent = "Sign in";
    }
  }
}

async function handleLogout() {
  try {
    await fetch("/auth/logout", { method: "POST" });
  } catch (_) {
    // Ignore errors — still show login
  }
  _currentUser = null;
  showLoginScreen();
}

// ── Health card ────────────────────────────────────────────────────────────

async function loadHealth() {
  const el = document.getElementById("card-health");
  const headerBadge = document.getElementById("header-health-badge");
  try {
    const data = await fetch("/health").then(r => r.json());
    const status = data.status || "unknown";
    const badge = statusBadge(status, STATUS_MAP);
    if (el) el.querySelector(".card__value").innerHTML = badge;
    if (headerBadge) headerBadge.innerHTML = badge;
  } catch (e) {
    if (el) el.querySelector(".card__value").innerHTML = statusBadge("error", STATUS_MAP);
  }
}

// ── Readiness card ─────────────────────────────────────────────────────────

async function loadReadiness() {
  const el = document.getElementById("card-readiness");
  if (!el) return;
  // Only admins can see readiness details; hide card for other roles
  if (!_currentUser || _currentUser.role !== "admin") {
    el.style.display = "none";
    return;
  }
  try {
    const data = await fetchJSON("/readiness");
    el.querySelector(".card__value").innerHTML = statusBadge(data.status || "unknown", STATUS_MAP);
  } catch (e) {
    if (e.message === "HTTP 401") return;
    el.querySelector(".card__value").innerHTML = statusBadge("error", STATUS_MAP);
  }
}

// ── Latest run card + panel ────────────────────────────────────────────────

async function loadLatestRun() {
  const cardEl = document.getElementById("card-run");
  const bodyEl = document.getElementById("run-panel-body");

  try {
    const data = await fetchJSON("/runs/latest");

    if (data.status === "empty" || !data.run_type) {
      if (cardEl) cardEl.querySelector(".card__value").innerHTML = statusBadge("empty", STATUS_MAP);
      if (bodyEl) bodyEl.innerHTML = `<p class="empty-state">No run history yet. Trigger a manual run or wait for the next scheduled run.</p>`;
      return;
    }

    const runStatus = data.status || "unknown";
    if (cardEl) cardEl.querySelector(".card__value").innerHTML = statusBadge(runStatus, STATUS_MAP);

    if (bodyEl) bodyEl.innerHTML = `
      <dl class="kv-list">
        <dt>Run type</dt>     <dd>${fmt(data.run_type)}</dd>
        <dt>Status</dt>       <dd>${statusBadge(runStatus, STATUS_MAP)}</dd>
        <dt>Started</dt>      <dd>${fmt(data.started_at)}</dd>
        <dt>Finished</dt>     <dd>${fmt(data.finished_at)}</dd>
        <dt>Report path</dt>  <dd>${fmt(data.report_path)}</dd>
        ${data.delivery_success !== undefined
          ? `<dt>Delivery</dt><dd>${statusBadge(data.delivery_success ? "success" : "failed", STATUS_MAP)}</dd>`
          : ""}
      </dl>`;
  } catch (e) {
    if (e.message === "HTTP 401") return;
    if (cardEl) cardEl.querySelector(".card__value").innerHTML = statusBadge("error", STATUS_MAP);
    if (bodyEl) bodyEl.innerHTML = `<p class="empty-state">Could not load run history.</p>`;
  }
}

// ── Latest report card + panel ─────────────────────────────────────────────

async function loadLatestReport() {
  const cardEl = document.getElementById("card-report");
  const bodyEl = document.getElementById("report-panel-body");
  const rawBtn = document.getElementById("btn-view-raw");

  try {
    const data = await fetchJSON("/reports/latest");

    if (!data.exists) {
      if (cardEl) cardEl.querySelector(".card__value").innerHTML = statusBadge("empty", STATUS_MAP);
      if (bodyEl) bodyEl.innerHTML = `<p class="empty-state">No report found yet. Reports appear after the first successful run.</p>`;
      if (rawBtn) rawBtn.style.display = "none";
      return;
    }

    if (cardEl) cardEl.querySelector(".card__value").innerHTML = statusBadge("available", {
      available: "badge--ok",
    });

    if (bodyEl) bodyEl.innerHTML = `
      <dl class="kv-list">
        <dt>Type</dt>      <dd>${fmt(data.report_type)}</dd>
        <dt>Filename</dt>  <dd>${fmt(data.filename)}</dd>
        <dt>Generated</dt> <dd>${fmt(data.generated_at)}</dd>
        <dt>Path</dt>      <dd>${fmt(data.path)}</dd>
      </dl>`;
  } catch (e) {
    if (e.message === "HTTP 401") return;
    if (cardEl) cardEl.querySelector(".card__value").innerHTML = statusBadge("error", STATUS_MAP);
    if (bodyEl) bodyEl.innerHTML = `<p class="empty-state">Could not load report metadata.</p>`;
    if (rawBtn) rawBtn.style.display = "none";
  }
}

// ── Raw report viewer ──────────────────────────────────────────────────────

async function loadRawReport() {
  const viewer = document.getElementById("report-raw-viewer");
  const btn = document.getElementById("btn-view-raw");

  if (viewer.classList.contains("visible")) {
    viewer.classList.remove("visible");
    btn.textContent = "View Raw Report";
    return;
  }

  btn.disabled = true;
  btn.textContent = "Loading…";
  viewer.classList.remove("visible");

  try {
    const text = await fetchText("/reports/latest/raw");
    viewer.textContent = text;
    viewer.classList.add("visible");
    btn.textContent = "Hide Raw Report";
  } catch (e) {
    if (e.message !== "HTTP 401") {
      viewer.textContent = "Could not load raw report. No markdown report may be available yet.";
      viewer.classList.add("visible");
    }
    btn.textContent = "View Raw Report";
  } finally {
    btn.disabled = false;
  }
}

// ── Scheduler status card + panel ──────────────────────────────────────────

async function loadSchedulerStatus() {
  const cardEl = document.getElementById("card-scheduler");
  const bodyEl = document.getElementById("scheduler-panel-body");

  try {
    const data = await fetchJSON("/scheduler/status");
    const isRunning = data.status === "running";
    const badgeCls = isRunning ? "badge--ok" : "badge--neutral";
    const label = isRunning ? "running" : (data.status || "unknown");

    if (cardEl) {
      cardEl.querySelector(".card__value").innerHTML =
        `<span class="badge ${badgeCls}"><span class="dot"></span>${escapeHtml(label)}</span>`;
    }

    if (bodyEl) {
      const jobs = Array.isArray(data.jobs) ? data.jobs : [];
      if (jobs.length === 0) {
        bodyEl.innerHTML = `<p class="empty-state">No scheduler jobs found.</p>`;
        return;
      }

      const rows = jobs.map(j => `
        <dt>${escapeHtml(j.job)}</dt>
        <dd>
          <span style="font-size:.85rem;color:var(--text-muted);">${escapeHtml(j.schedule || "—")}</span>
          &nbsp;·&nbsp; next run: <strong>${fmt(j.next_run)}</strong>
        </dd>`).join("");

      bodyEl.innerHTML = `<dl class="kv-list">${rows}</dl>`;
    }
  } catch (e) {
    if (e.message === "HTTP 401") return;
    if (cardEl) {
      cardEl.querySelector(".card__value").innerHTML = statusBadge("error", STATUS_MAP);
    }
    if (bodyEl) {
      bodyEl.innerHTML = `<p class="empty-state">Could not load scheduler status.</p>`;
    }
  }
}

// ── Manual run trigger (admin only) ───────────────────────────────────────

async function triggerRun(jobType) {
  const feedbackEl = document.getElementById("run-feedback");
  const buttons = document.querySelectorAll(".run-buttons .btn");

  buttons.forEach(b => { b.disabled = true; });
  showFeedback(feedbackEl, "loading", `<span class="spinner"></span> Running ${jobType} job — this may take a minute…`);

  try {
    const res = await fetch(`/run/${jobType}`, {
      method: "POST",
      credentials: "same-origin",
    });

    const data = await res.json();

    if (res.ok && data.status === "success") {
      const rp = data.result && data.result.report_path ? ` Report: ${escapeHtml(data.result.report_path)}` : "";
      showFeedback(feedbackEl, "success",
        `✓ ${escapeHtml(jobType)} run completed. Finished at ${escapeHtml(data.finished_at || "—")}.${rp}`);
      await Promise.all([loadLatestRun(), loadLatestReport()]);
    } else if (res.status === 401) {
      showFeedback(feedbackEl, "error", "Session expired — please sign in again.");
      showLoginScreen();
    } else if (res.status === 403) {
      showFeedback(feedbackEl, "error", "Access denied — admin role required.");
    } else if (res.status === 409) {
      showFeedback(feedbackEl, "error", `${escapeHtml(jobType)} job is already running. Wait for it to finish.`);
    } else {
      const errMsg = data.error || data.detail || "Unknown error";
      showFeedback(feedbackEl, "error", `Run failed: ${escapeHtml(errMsg)}`);
    }
  } catch (e) {
    showFeedback(feedbackEl, "error", `Request failed: ${escapeHtml(e.message)}`);
  } finally {
    buttons.forEach(b => { b.disabled = false; });
  }
}

function showFeedback(el, type, html) {
  if (!el) return;
  el.className = `run-feedback run-feedback--${type} visible`;
  el.innerHTML = html;
}

// ── Load all dashboard data ────────────────────────────────────────────────

function loadAll() {
  loadHealth();
  loadReadiness();
  loadLatestRun();
  loadLatestReport();
  loadSchedulerStatus();
}

// ── Wire up event listeners ────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", async () => {
  // Check auth first
  const loginForm = document.getElementById("login-form");
  if (loginForm) loginForm.addEventListener("submit", handleLogin);

  const logoutBtn = document.getElementById("btn-logout");
  if (logoutBtn) logoutBtn.addEventListener("click", handleLogout);

  const isAuth = await checkAuth();
  if (!isAuth) return;

  // Load dashboard data
  loadAll();

  // Raw report toggle
  const rawBtn = document.getElementById("btn-view-raw");
  if (rawBtn) rawBtn.addEventListener("click", loadRawReport);

  // Run buttons (only visible to admin, but wire up regardless)
  const dailyBtn = document.getElementById("btn-run-daily");
  const weeklyBtn = document.getElementById("btn-run-weekly");
  const monthlyBtn = document.getElementById("btn-run-monthly");
  if (dailyBtn) dailyBtn.addEventListener("click", () => triggerRun("daily"));
  if (weeklyBtn) weeklyBtn.addEventListener("click", () => triggerRun("weekly"));
  if (monthlyBtn) monthlyBtn.addEventListener("click", () => triggerRun("monthly"));
});
