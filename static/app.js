/**
 * static/app.js
 *
 * Logistaas Ads Intelligence Dashboard — frontend logic.
 *
 * Rules:
 *  - No hardcoded secrets.
 *  - No external tracking or analytics.
 *  - No third-party JS dependencies.
 *  - Token stored in sessionStorage only (cleared on tab close).
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

// ── Fetch wrappers ─────────────────────────────────────────────────────────

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function fetchText(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.text();
}

// ── Health card ────────────────────────────────────────────────────────────

async function loadHealth() {
  const el = document.getElementById("card-health");
  const headerBadge = document.getElementById("header-health-badge");
  try {
    const data = await fetchJSON("/health");
    const status = data.status || "unknown";
    const badge = statusBadge(status, STATUS_MAP);
    el.querySelector(".card__value").innerHTML = badge;
    if (headerBadge) headerBadge.innerHTML = badge;
  } catch (e) {
    el.querySelector(".card__value").innerHTML = statusBadge("error", STATUS_MAP);
  }
}

// ── Readiness card ─────────────────────────────────────────────────────────

async function loadReadiness() {
  const el = document.getElementById("card-readiness");
  try {
    const data = await fetchJSON("/readiness");
    el.querySelector(".card__value").innerHTML = statusBadge(data.status || "unknown", STATUS_MAP);
  } catch (e) {
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
      cardEl.querySelector(".card__value").innerHTML = statusBadge("empty", STATUS_MAP);
      bodyEl.innerHTML = `<p class="empty-state">No run history yet. Trigger a manual run or wait for the next scheduled run.</p>`;
      return;
    }

    const runStatus = data.status || "unknown";
    cardEl.querySelector(".card__value").innerHTML = statusBadge(runStatus, STATUS_MAP);

    bodyEl.innerHTML = `
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
    cardEl.querySelector(".card__value").innerHTML = statusBadge("error", STATUS_MAP);
    bodyEl.innerHTML = `<p class="empty-state">Could not load run history.</p>`;
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
      cardEl.querySelector(".card__value").innerHTML = statusBadge("empty", STATUS_MAP);
      bodyEl.innerHTML = `<p class="empty-state">No report found yet. Reports appear after the first successful run.</p>`;
      if (rawBtn) rawBtn.style.display = "none";
      return;
    }

    cardEl.querySelector(".card__value").innerHTML = statusBadge("available", {
      available: "badge--ok",
    });

    bodyEl.innerHTML = `
      <dl class="kv-list">
        <dt>Type</dt>      <dd>${fmt(data.report_type)}</dd>
        <dt>Filename</dt>  <dd>${fmt(data.filename)}</dd>
        <dt>Generated</dt> <dd>${fmt(data.generated_at)}</dd>
        <dt>Path</dt>      <dd>${fmt(data.path)}</dd>
      </dl>`;
  } catch (e) {
    cardEl.querySelector(".card__value").innerHTML = statusBadge("error", STATUS_MAP);
    bodyEl.innerHTML = `<p class="empty-state">Could not load report metadata.</p>`;
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
    viewer.textContent = "Could not load raw report. No markdown report may be available yet.";
    viewer.classList.add("visible");
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
    if (cardEl) {
      cardEl.querySelector(".card__value").innerHTML = statusBadge("error", STATUS_MAP);
    }
    if (bodyEl) {
      bodyEl.innerHTML = `<p class="empty-state">Could not load scheduler status.</p>`;
    }
  }
}

function getToken() {
  const inputEl = document.getElementById("admin-token-input");
  const val = inputEl ? inputEl.value.trim() : "";
  if (val) {
    sessionStorage.setItem("_adm_tok", val);
    return val;
  }
  return sessionStorage.getItem("_adm_tok") || "";
}

function clearToken() {
  sessionStorage.removeItem("_adm_tok");
  const inputEl = document.getElementById("admin-token-input");
  if (inputEl) inputEl.value = "";
}

// ── Manual run trigger ─────────────────────────────────────────────────────

async function triggerRun(jobType) {
  const feedbackEl = document.getElementById("run-feedback");
  const buttons = document.querySelectorAll(".run-buttons .btn");

  const token = getToken();
  if (!token) {
    showFeedback(feedbackEl, "error", "Please enter your ADMIN_API_TOKEN before triggering a run.");
    return;
  }

  buttons.forEach(b => { b.disabled = true; });
  showFeedback(feedbackEl, "loading", `<span class="spinner"></span> Running ${jobType} job — this may take a minute…`);

  try {
    const res = await fetch(`/run/${jobType}`, {
      method: "POST",
      headers: { "Authorization": `Bearer ${token}` },
    });

    const data = await res.json();

    if (res.ok && data.status === "success") {
      const rp = data.result && data.result.report_path ? ` Report: ${escapeHtml(data.result.report_path)}` : "";
      showFeedback(feedbackEl, "success",
        `✓ ${escapeHtml(jobType)} run completed. Finished at ${escapeHtml(data.finished_at || "—")}.${rp}`);
      // Refresh panels after a successful run.
      await Promise.all([loadLatestRun(), loadLatestReport()]);
    } else if (res.status === 401) {
      showFeedback(feedbackEl, "error", "Unauthorized — token is missing or incorrect.");
    } else if (res.status === 409) {
      showFeedback(feedbackEl, "error", `${escapeHtml(jobType)} job is already running. Wait for it to finish.`);
    } else if (res.status === 503) {
      showFeedback(feedbackEl, "error", "ADMIN_API_TOKEN is not configured on the server.");
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
  el.className = `run-feedback run-feedback--${type} visible`;
  el.innerHTML = html;
}

// ── Wire up event listeners ────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  // Load all data sections.
  loadHealth();
  loadReadiness();
  loadLatestRun();
  loadLatestReport();
  loadSchedulerStatus();

  // Restore token from sessionStorage (page reload within same tab).
  const saved = sessionStorage.getItem("_adm_tok");
  if (saved) {
    const inp = document.getElementById("admin-token-input");
    if (inp) inp.value = saved;
  }

  // Run buttons.
  document.getElementById("btn-run-daily")
    .addEventListener("click", () => triggerRun("daily"));
  document.getElementById("btn-run-weekly")
    .addEventListener("click", () => triggerRun("weekly"));
  document.getElementById("btn-run-monthly")
    .addEventListener("click", () => triggerRun("monthly"));

  // Raw report toggle.
  document.getElementById("btn-view-raw")
    .addEventListener("click", loadRawReport);

  // Token clear button.
  document.getElementById("btn-clear-token")
    .addEventListener("click", () => {
      clearToken();
      const feedbackEl = document.getElementById("run-feedback");
      showFeedback(feedbackEl, "success", "Token cleared from session.");
      setTimeout(() => { feedbackEl.className = "run-feedback"; }, 2000);
    });
});
