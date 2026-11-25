#!/usr/bin/env python3
"""
scripts/audit_campaigns_page_certification.py

PR-ADS-177C — READ-ONLY certification gate for the live Campaigns surfaces:
the one Campaigns page and Dashboard → Campaigns, which both read the Revenue
Decision Mart's SETTLED campaign view, and the campaign drawer that lists a
row's closed-won deals. It replaces
``scripts/audit_campaign_evidence_certification.py`` (PR-ADS-157), which
certified the retired Campaign Evidence page, in the daily platform
certification (``scripts/audit_platform_certification.AUDITS``).

    python -m scripts.audit_campaigns_page_certification
    python -m scripts.audit_campaigns_page_certification --window ytd --json
    echo $?     # 0 = certified · 1 = truth-contract violation · 2 = unavailable

What it checks (each line names the PR-ADS-157 check it ports; ``docs/60`` §9)
--------------------------------------------------------------------------
Static (no database):

  S1  retired_readers_absent — no retired route (/api/campaigns,
      /api/campaign-detail, /api/keywords, /api/search-terms*,
      /api/revenue-attribution) is registered, no retired service module
      exists, and the UI requests none of them. (new: the retirement holds)
  S2  legacy_readers — the live campaign builders hold no legacy ``keywords``
      / ``waste_terms`` read and no waste_terms metric. (157 checks 2, 3)
  S3  account_scope — the keyword repository's account predicate is applied
      in SQL, an empty candidate list selects nothing, and the account-wide
      read is unchanged. (157 checks 16, 17)
  S4  frontend_gates — every leads / SQLs figure on the Campaigns page, its
      drawer and Dashboard → Campaigns passes through ``cohortCountHtml`` with
      the response's OWN cohort as an argument; no campaign renderer prints a
      CPQL; the strict spend gate returns before any figure. (157 check 15)

Live, per business window (the Campaigns page's windows):

  L1  build — the settled campaign view and Dashboard → Campaigns build.
      A build that raises is exit 2, never a pass. (157 check 1)
  L2  withheld_not_exposed — a cohort verdict that is not ``published``
      leaves no lead / SQL count, breakdown or residual count anywhere in
      either payload. (157 checks 9, 10)
  L3  conservation — under a published cohort, Σ campaign rows + the
      residual = the cohort's Google Ads total = ``campaign_totals``, for
      leads and SQLs, and Dashboard → Campaigns' KPIs equal it. (157 check 11)
  L4  identity_unique — no two campaign rows share a key. (157 check 12)
  L5  shared_name_not_merged — a row whose name more than one Google Ads
      campaign id spent under says so (``identity_status``), lists the ids,
      and is not judged as one campaign; Dashboard → Campaigns marks it for
      mapping review; Overview's best-campaign and waste signals never name
      it (PR-ADS-177C-F1). (157 checks 13, 14)
  L6  drawer_population — every row's drawer population, read ONCE per window
      (``campaign_deal_details_by_campaign(settled_days=True)``, what every
      drawer answers from), lists exactly the row's ``deal_ids``; the number
      of drawers probed is recorded; exit 2 when canonical revenue cannot be
      read. (New in PR-ADS-177C — 157's checks 4–8 certified the retired
      drawer previews and are not ported.)
  L7  spend_gate — when campaign spend is not decision-ready, no spend, USD
      spend or period ratio is published on a row, the totals or the spend
      truth. (PR-ADS-177C M4)
  L8  no_cpql — no cost-per-SQL key anywhere in either payload.

Guarantees
----------
  * NO writes of any kind. Every database access is a SELECT through the
    production read-only builders.
  * NO external API calls. Google Ads, HubSpot and Meta are never contacted.
  * NO contact PII is printed.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

EXIT_CERTIFIED = 0
EXIT_VIOLATION = 1
EXIT_UNAVAILABLE = 2

#: The routes PR-ADS-177C retired. Each answers 404; none may come back.
RETIRED_ROUTES = (
    "/api/campaigns", "/api/campaign-detail", "/api/keywords",
    "/api/search-terms", "/api/search-terms/summary", "/api/search-terms/ngrams",
    "/api/revenue-attribution",
)
#: PR-ADS-177C-F1: where a campaign row is printed, and the shared-name
#: disclosure helpers each surface must call.
JS_SHARED_NAME_SURFACES = {
    "renderCampaignsPageTable": ("campaignSharedNameHtml", "campaignSharedRatioNote"),
    "renderCampaignsPageDrawer": ("campaignSharedIdsHtml", "campaignSharedRatioNote"),
    "renderCampLeaderboard": ("campaignSharedNameHtml", "campaignSharedRatioNote"),
    "wireCampDrawers": ("campaignSharedIdsHtml", "campaignSharedRatioNote"),
    # PR-ADS-177C-F2 labelled Dashboard → Deals' Campaign → Pipeline table
    # here; PR-ADS-178B deleted that table (Dashboard → Deals republishes the
    # Pipeline page, which has no per-campaign rows), so there is nothing left
    # to label.
}

#: PR-ADS-177C: a row whose name several Google Ads campaign ids share.
SHARED_NAME = "campaign_name_shared_by_google_ads_campaigns"
#: Dashboard → Campaigns' status for it (``STATUS_MAPPING_REVIEW``).
DASH_MAPPING_REVIEW = "Mapping needs review"

#: The service modules PR-ADS-177C retired.
RETIRED_MODULES = ("services/campaign_evidence_service.py",)

#: The live campaign builders (file, function or None for the whole module).
LIVE_CAMPAIGN_BUILDERS = (
    ("services/dashboard_campaigns_service.py", None),
    ("services/revenue_decision_mart.py", None),
    ("services/revenue_attribution_service.py", "build_campaign_deal_details"),
)
_LEGACY_TOKENS = ("FROM keywords", "FROM waste_terms", "fetch_keyword_theme_snapshot",
                  "waste_terms.spend_usd", "crm_junk_confirmed")

#: The JS renderers of the live Campaigns surfaces that print leads / SQLs.
JS_COHORT_SURFACES = (
    "renderCampaignsPageSummary", "renderCampaignsPageTable",
    "renderCampaignsResidualRow", "renderCampaignsPageDrawer",
    "renderCampLeaderboard", "renderCampKpiRow",
)
_JS_CPQL = re.compile(r"cpql", re.I)

_APP_JS = _ROOT / "static" / "app.js"
_INDEX = _ROOT / "static" / "index.html"
_API = _ROOT / "api" / "server.py"


class Findings:
    """Violations and unavailability, kept apart (as in every gate here)."""

    def __init__(self) -> None:
        self.violations: list[str] = []
        self.unavailable: list[str] = []
        self.checks: list[dict] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})

    def violation(self, name: str, detail: str) -> None:
        self.violations.append(f"{name}: {detail}")
        self.record(name, False, detail)

    def unavailable_now(self, name: str, detail: str) -> None:
        self.unavailable.append(f"{name}: {detail}")
        self.record(name, False, detail)

    def passed(self, name: str, detail: str = "") -> None:
        self.record(name, True, detail)

    @property
    def exit_code(self) -> int:
        if self.violations:
            return EXIT_VIOLATION
        if self.unavailable:
            return EXIT_UNAVAILABLE
        return EXIT_CERTIFIED


# ─────────────────────────────────────────────────────────────────────────────
# Static checks
# ─────────────────────────────────────────────────────────────────────────────
def _code_only(path: Path, function: str | None = None) -> str | None:
    """A module's (or one function's) code with docstrings and comments
    stripped — prose that NAMES a forbidden read is not a read."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if function is not None:
        node = next((n for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name == function), None)
        if node is None:
            return None
        tree = node
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = n.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(getattr(body[0], "value", None), ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body[0].value.value = ""
    return ast.unparse(tree)


def _js_code_only(src: str) -> str:
    """JS / HTML with comments removed (a comment that NAMES a retired route
    to say it is retired is not a request). String contents are kept."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def _route_paths(src: str) -> set[str]:
    return set(re.findall(r'@app\.(?:get|post|put|delete|patch)\(\s*"([^"]+)"', src))


def check_retired_readers_absent(f: Findings, *, root: Path = _ROOT) -> None:
    """S1 — the retirement holds: no route, no module, no UI request."""
    problems = []
    try:
        routes = _route_paths((root / "api" / "server.py").read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        f.unavailable_now("retired_readers_absent", f"api/server.py unreadable: {exc}")
        return
    problems += [f"route {r} is registered" for r in RETIRED_ROUTES if r in routes]
    problems += [f"{m} exists" for m in RETIRED_MODULES if (root / m).exists()]
    for name in ("static/app.js", "static/index.html"):
        try:
            text = _js_code_only((root / name).read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            f.unavailable_now("retired_readers_absent", f"{name} unreadable: {exc}")
            return
        for r in RETIRED_ROUTES:
            # The exact path, followed by a query string, a quote or a
            # backtick — never a longer live path that starts with it
            # (/api/revenue-attribution/audit is live).
            if re.search(re.escape(r) + r"(?=[?`'\"])", text):
                problems.append(f"{name} requests {r}")
    for py in ("api", "services", "scripts", "db", "analysis", "scheduler"):
        for path in sorted((root / py).rglob("*.py")):
            code = path.read_text(encoding="utf-8", errors="replace")
            if re.search(r"\bcampaign_evidence_service\b", code) and re.search(
                    r"(?m)^\s*(from\s+services(\.|\s+import\s+)campaign_evidence_service"
                    r"|import\s+services\.campaign_evidence_service)", code):
                problems.append(f"{path.relative_to(root)} imports campaign_evidence_service")
    if problems:
        f.violation("retired_readers_absent", "; ".join(problems[:8]))
    else:
        f.passed("retired_readers_absent",
                 f"{len(RETIRED_ROUTES)} retired routes unregistered and unrequested; "
                 "the retired service is gone and imported nowhere")


def check_legacy_readers(f: Findings, *, root: Path = _ROOT) -> None:
    """S2 — no legacy snapshot read, no waste_terms metric, in a live builder."""
    offenders = []
    for rel, fn in LIVE_CAMPAIGN_BUILDERS:
        code = _code_only(root / rel, fn)
        if code is None:
            f.violation("legacy_readers", f"{rel}{':' + fn if fn else ''} not found")
            return
        hits = [t for t in _LEGACY_TOKENS if t in code]
        if hits:
            offenders.append(f"{rel}{':' + fn if fn else ''}: {hits}")
    if offenders:
        f.violation("legacy_readers", "; ".join(offenders))
    else:
        f.passed("legacy_readers", "no live campaign builder reads the legacy keyword "
                 "snapshot or takes a metric from waste_terms")


def check_account_scope(f: Findings, *, root: Path = _ROOT) -> None:
    """S3 — the keyword repository's account predicate (157 §1)."""
    repo = root / "db" / "keyword_repository.py"
    try:
        src = repo.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        f.unavailable_now("account_scope", f"{repo} unreadable: {exc}")
        return
    if '_ACCOUNT = "customer_id = ANY(%s)"' not in src:
        f.violation("account_scope", "db/keyword_repository.py has no account predicate")
        return
    missing = [fn for fn in ("fetch_keyword_aggregates", "fetch_keyword_daily_costs")
               if "_scope(customer_ids)" not in (_code_only(repo, fn) or "")]
    if missing:
        f.violation("account_scope", f"keyword reads that do not apply the account "
                    f"scope in SQL: {missing}")
        return
    try:
        import db.keyword_repository as kw_repo  # noqa: PLC0415
        empty_sql, empty_params = kw_repo._scope([])
        wide_sql, _ = kw_repo._scope(None)
    except Exception as exc:  # noqa: BLE001
        f.violation("account_scope", f"the scope helper could not be exercised: {exc}")
        return
    if "customer_id = ANY" not in empty_sql or empty_params != ([],):
        f.violation("account_scope", "an empty account candidate list does not apply "
                    "the predicate — it would widen to every account")
    elif "customer_id" in wide_sql:
        f.violation("account_scope", "the account-wide read gained an account "
                    "predicate, which would silently change the Keywords tab")
    else:
        f.passed("account_scope", "account predicate applied in SQL before "
                 "aggregation; an empty candidate list selects nothing")


def _js_function(js: str, name: str) -> str:
    i = js.find(f"function {name}(")
    if i == -1:
        return ""
    j = js.find("\nfunction ", i + 10)
    k = js.find("\nasync function ", i + 10)
    ends = [e for e in (j, k) if e != -1]
    return js[i:min(ends) if ends else len(js)]


def check_frontend_gates(f: Findings, *, js: str | None = None) -> None:
    """S4 — every leads / SQLs figure goes through the cohort gate with its
    response's OWN cohort; no CPQL; the strict spend gate comes first."""
    try:
        js = js if js is not None else _APP_JS.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        f.unavailable_now("frontend_gates", f"static/app.js unreadable: {exc}")
        return
    problems = []
    for fn in JS_COHORT_SURFACES:
        body = _js_function(js, fn)
        if not body:
            problems.append(f"{fn} not found")
            continue
        if "cohortCountHtml(" not in body:
            problems.append(f"{fn} prints no gated cohort figure")
            continue
        for call in re.findall(r"cohortCountHtml\(([^;]*?)\)", body):
            args = [a.strip() for a in call.split(",")]
            if len(args) < 2 or not args[1] or args[1] in ("null", "undefined", "{}") \
                    or args[1].startswith(("{", "[", '"', "'")):
                problems.append(f"{fn}: cohortCountHtml({call}) is not given a "
                                "response's cohort")
        if _JS_CPQL.search(body):
            problems.append(f"{fn} prints a CPQL")
    # PR-ADS-177C-F1 (truth audit M-1): a shared-name row is labelled where it
    # is printed — beside its name, its period ratio and in its drawer — and
    # its badge says why it is not judged.
    for fn, helpers in JS_SHARED_NAME_SURFACES.items():
        body = _js_function(js, fn)
        for helper in helpers:
            if f"{helper}(" not in body:
                problems.append(f"{fn} prints a campaign row without {helper}")
    if "CAMPAIGN_SHARED_NAME" not in _js_function(js, "getBusinessVerdictBadge"):
        problems.append("getBusinessVerdictBadge has no words of its own for a shared name")
    gate = _js_function(js, "campaignSqlPublication")
    if "function campaignSqlPublication(cohort)" not in gate:
        problems.append("campaignSqlPublication does not take the cohort as its argument")
    page = _js_function(js, "renderCampaignsPage")
    g = page.find("spend_decision_ready !== true")
    s = page.find("renderCampaignsPageSummary(")
    if g == -1 or s == -1 or g > s:
        problems.append("the Campaigns page's strict spend gate does not return "
                        "before the KPI cards")
    if problems:
        f.violation("frontend_gates", "; ".join(problems[:8]))
    else:
        f.passed("frontend_gates", f"all {len(JS_COHORT_SURFACES)} campaign surfaces "
                 "gate every leads / SQLs figure on their own response's cohort; "
                 "no CPQL; the spend gate comes first")


# ─────────────────────────────────────────────────────────────────────────────
# Live checks — per business window, against the real builders
# ─────────────────────────────────────────────────────────────────────────────
def _jsonable(payload):
    return json.loads(json.dumps(payload, default=str))


def _cpql_keys(node, path="") -> list[str]:
    from scripts.audit_marketing_outcome_cohorts import _cpql_keys as walk  # noqa: PLC0415
    return walk(node, path)


def spend_exposures(payload: dict) -> list[str]:
    """Every spend figure still published on a settled campaign payload whose
    spend is NOT decision-ready (PR-ADS-177C M4). Empty when it is ready."""
    if ((payload.get("readiness") or {}).get("spend_decision_ready")) is True:
        return []
    out = []
    for i, r in enumerate(payload.get("rows") or []):
        name = r.get("campaign_name") or i
        for k in ("spend", "period_revenue_per_ad_spend"):
            if r.get(k) is not None:
                out.append(f"rows[{name}].{k}")
    ct = payload.get("campaign_totals") or {}
    out += [f"campaign_totals.{k}" for k in ("spend", "period_revenue_per_ad_spend")
            if ct.get(k) is not None]
    s = payload.get("summary") or {}
    out += [f"summary.{k}" for k in ("spend_usd",) if s.get(k) is not None]
    st = payload.get("spend_truth") or {}
    out += [f"spend_truth.{k}" for k in ("usd_spend",) if st.get(k) is not None]
    return out


def audit_window(window: str, f: Findings, *, now=None) -> dict:
    """L1–L8 for one business window."""
    from scripts.audit_marketing_outcome_cohorts import (  # noqa: PLC0415
        mart_conservation_problems, mart_withheld_exposures,
    )
    w = f"[{window}]"
    out: dict = {"window": window}
    try:
        from services.dashboard_campaigns_service import build_dashboard_campaigns  # noqa: PLC0415
        from services.revenue_decision_mart import build_revenue_decision_mart  # noqa: PLC0415
        page = _jsonable(build_revenue_decision_mart(view="campaign", window=window,
                                                     now=now, settled=True))
        dash = _jsonable(build_dashboard_campaigns(window=window, now=now))
    except MemoryError:
        raise
    except Exception as exc:  # noqa: BLE001
        f.unavailable_now(f"build{w}", f"a campaign surface could not be built: "
                          f"{type(exc).__name__}: {exc}"[:300])
        return {**out, "available": False}
    f.passed(f"build{w}", "the settled campaign view and Dashboard → Campaigns built")

    cohort = page.get("cohort") or {}
    status = cohort.get("sql_status")
    out["sql_status"] = status

    # L2
    exposed = mart_withheld_exposures(page) + [f"dashboard:{x}" for x in
                                               mart_withheld_exposures(dash)]
    if exposed:
        f.violation(f"withheld_not_exposed{w}",
                    f"cohort {status!r} but a count is present at {exposed[:6]}")
    else:
        f.passed(f"withheld_not_exposed{w}",
                 "published" if status == "published" else
                 f"cohort {status} ({cohort.get('sql_reason')}); nothing exposed")

    # L3
    problems = mart_conservation_problems(page, view="campaign")
    if status == "published":
        want = (cohort.get("totals") or {}).get("sqls")
        dk = (dash.get("kpis") or {}).get("sqls")
        if (dash.get("cohort") or {}).get("sql_status") == "published" and dk != want:
            problems.append(f"Dashboard → Campaigns kpis.sqls {dk} != cohort total {want}")
        for field in ("leads", "sqls"):
            rs = [r.get(field) for r in page.get("rows") or []]
            if any(v is None for v in rs):
                problems.append(f"a campaign row carries no {field} under a published cohort")
    if problems:
        f.violation(f"conservation{w}", "; ".join(problems[:5]))
    elif status == "published":
        f.passed(f"conservation{w}", "rows + residual = cohort total = campaign_totals "
                 "= Dashboard KPIs")
    elif status not in ("withheld",):
        # "unavailable" is a failed funnel read, not a gate the page applied:
        # nothing about the SQL counts was proven. Exit 2, never a pass.
        f.unavailable_now(f"conservation{w}", f"the cohort could not be read "
                          f"({status}: {cohort.get('sql_reason')}); conservation unproven")

    # L4
    keys = [r.get("campaign_id") for r in page.get("rows") or []]
    dupes = sorted({k for k in keys if keys.count(k) > 1}, key=str)
    if dupes:
        f.violation(f"identity_unique{w}", f"row keys on more than one row: {dupes[:5]}")
    else:
        f.passed(f"identity_unique{w}", f"{len(keys)} distinct campaign rows")

    # L5 — the settled view, Dashboard → Campaigns' rows and (PR-ADS-177C-F1)
    # Overview's campaign signals.
    bad = []
    shared = 0
    shared_names = set()
    for r in page.get("rows") or []:
        ids = r.get("google_ads_campaign_ids")
        if ids is None:
            bad.append(f"{r.get('campaign_name')}: no google_ads_campaign_ids")
        elif len(ids) > 1:
            shared += 1
            shared_names.add((r.get("campaign_name") or "").strip().lower())
            if r.get("identity_status") != SHARED_NAME \
                    or r.get("verdict") != "mapping_required":
                bad.append(f"{r.get('campaign_name')}: {len(ids)} campaign ids presented "
                           "as one campaign")
    for r in dash.get("campaigns") or []:
        ids = r.get("google_ads_campaign_ids") or []
        name = (r.get("campaign_name") or "").strip().lower()
        if len(ids) > 1 or name in shared_names:
            shared_names.add(name)
            if r.get("identity_status") != SHARED_NAME \
                    or r.get("status") != DASH_MAPPING_REVIEW:
                bad.append(f"dashboard:{r.get('campaign_name')}: a shared name presented "
                           "as one campaign")
    try:
        from services.dashboard_overview_service import build_dashboard_overview  # noqa: PLC0415
        overview = _jsonable(build_dashboard_overview(window=window, now=now))
        # Overview reads the mart's through-today campaign view; its rows are
        # the reference for Overview's totals below.
        today_view = _jsonable(build_revenue_decision_mart(view="campaign", window=window,
                                                           now=now))
    except MemoryError:
        raise
    except Exception as exc:  # noqa: BLE001
        overview = today_view = None
        f.unavailable_now(f"shared_name_not_merged{w}",
                          f"Overview could not be built: {type(exc).__name__}")
    if overview is not None:
        bad += overview_shared_name_problems(overview, today_view)
    if bad:
        f.violation(f"shared_name_not_merged{w}", "; ".join(bad[:5]))
    else:
        f.passed(f"shared_name_not_merged{w}",
                 f"{shared} name(s) shared by more than one campaign id, each disclosed "
                 "and not judged as one campaign" if shared else
                 "no name is shared by two campaign ids in this window")

    # L6 — PR-ADS-177C-F1: the drawers' population is read ONCE per window
    # (``campaign_deal_details_by_campaign``, the function every campaign
    # drawer answers from) and every row is checked against it — including a
    # row whose deal_ids is [] (its drawer must list nothing). The count of
    # drawers probed is recorded, so a pass over no drawer is visible; a
    # ledger that cannot be read proves nothing and is exit 2.
    out["drawers_probed"] = None
    out["rows_with_deals"] = None
    out["canonical_revenue"] = None
    try:
        from services.revenue_attribution_service import (  # noqa: PLC0415
            _norm, campaign_deal_details_by_campaign,
        )
        grouped = campaign_deal_details_by_campaign(window, now, settled_days=True)
    except MemoryError:
        raise
    except Exception as exc:  # noqa: BLE001
        f.unavailable_now(f"drawer_population{w}",
                          f"the drawers could not be built: {type(exc).__name__}")
    else:
        if not grouped.get("available"):
            out["canonical_revenue"] = grouped.get("reason") or "unavailable"
            f.unavailable_now(f"drawer_population{w}",
                              "canonical revenue is unavailable "
                              f"({out['canonical_revenue']}); no drawer can be proven")
        else:
            out["canonical_revenue"] = "available"
            by_campaign = grouped.get("by_campaign") or {}
            mismatched, probed, with_deals = [], 0, 0
            for r in page.get("rows") or []:
                ids = r.get("deal_ids")
                if ids is None:
                    continue            # withheld: no proof list, nothing to compare
                probed += 1
                with_deals += 1 if ids else 0
                got = sorted(str(x.get("deal_record_id"))
                             for x in by_campaign.get(_norm(r.get("campaign_name")), []))
                if got != sorted(str(i) for i in ids):
                    mismatched.append(f"{r.get('campaign_name')}: drawer {len(got)} "
                                      f"vs row {len(ids)}")
            out["drawers_probed"], out["rows_with_deals"] = probed, with_deals
            if mismatched:
                f.violation(f"drawer_population{w}", "; ".join(mismatched[:5]))
            else:
                f.passed(f"drawer_population{w}",
                         f"{probed} drawer(s) probed ({with_deals} with deals); each "
                         "lists exactly its row's deal_ids")

    # L7
    leaked = spend_exposures(page)
    if leaked:
        f.violation(f"spend_gate{w}", f"spend is not decision-ready but published at "
                    f"{leaked[:6]}")
    else:
        f.passed(f"spend_gate{w}", "no spend figure over unsettled spend")

    # L8
    cpql = _cpql_keys(page) + [f"dashboard:{k}" for k in _cpql_keys(dash)]
    if cpql:
        f.violation(f"no_cpql{w}", f"a cost-per-SQL key is present: {cpql[:5]}")
    else:
        f.passed(f"no_cpql{w}", "no CPQL key")
    return {**out, "available": True, "rows": len(page.get("rows") or [])}


def overview_shared_name_problems(overview: dict, today_view: dict) -> list[str]:
    """PR-ADS-177C-F1/F2 — Overview's campaign signals and decision cards
    against the rows they are computed from (the mart's through-today campaign
    view). A shared-name row's figures are the name's true combined figures:

    * they stay in every total and count — the waste signal's value and
      campaign count, and the Scale card's producers (F2: F1 dropped them, so
      a shared row that was the only producer read "No verified won-deal
      producers yet" and its waste vanished);
    * a signal or card that NAMES one row names a shared row only with its
      label ("… share this name — figures combined").
    """
    out: list[str] = []
    rows = [r for r in today_view.get("rows") or [] if not r.get("is_residual")]
    shared = {(r.get("campaign_name") or "").strip().lower(): r for r in rows
              if r.get("identity_status") == SHARED_NAME}
    signals = overview.get("top_signals") or {}
    cards = overview.get("decision_cards") or []

    def _label(row: dict) -> str:
        """The EXACT label a shared row must carry (carried minor 3, round 3 —
        a loose "share" substring passed a label with the wrong count)."""
        ids = row.get("google_ads_campaign_ids") or []
        n = str(len(ids)) if len(ids) > 1 else "Several"
        return f"{n} Google Ads campaigns share this name — figures combined"

    def _unlabelled(text: str) -> list[str]:
        """Shared names that appear in ``text`` without being followed by
        exactly `` (<their label>)``."""
        low, bad = (text or "").lower(), []
        for n, row in shared.items():
            if not n:
                continue
            want = f" ({_label(row)})".lower()
            start = low.find(n)
            while start != -1:
                if not low.startswith(want, start + len(n)):
                    bad.append(n)
                    break
                start = low.find(n, start + len(n))
        return bad

    for key, field, label_field in (("best_campaign", "label", "shared_name_label"),
                                    ("biggest_waste", "top_campaign",
                                     "top_campaign_shared_name_label")):
        sig = signals.get(key) or {}
        row = shared.get(str(sig.get(field) or "").strip().lower())
        if row is not None and sig.get(label_field) != _label(row):
            out.append(f"overview:{key} names {sig.get(field)!r}, a name several Google "
                       f"Ads campaigns share, without its label ({sig.get(label_field)!r})")
    for card in cards:
        text = f"{card.get('headline') or ''} {card.get('body') or ''}"
        if _unlabelled(text):
            out.append(f"overview:{card.get('type')} card names a shared name "
                       f"{_unlabelled(text)} without its exact label")
    # Totals keep the shared rows.
    if (today_view.get("summary") or {}).get("sqls") is not None:
        wr = [r for r in rows if (r.get("spend") or 0) > 0 and (r.get("sqls") or 0) == 0
              and r.get("closed_won_deals") == 0]
        sig = signals.get("biggest_waste")
        if wr and not sig:
            out.append(f"overview:biggest_waste is absent over {len(wr)} campaign row(s) "
                       "with spend and no SQL or won-deal proof")
        elif wr and sig:
            want = round(sum(float(r.get("spend") or 0.0) for r in wr), 2)
            if sig.get("campaign_count") != len(wr) or \
                    abs(float(sig.get("value") or 0.0) - want) > 0.01:
                out.append(f"overview:biggest_waste {sig.get('value')} over "
                           f"{sig.get('campaign_count')} row(s) != {want} over {len(wr)}")
    readiness = overview.get("readiness") or {}
    producing = [r for r in rows if (r.get("closed_won_deals") or 0) > 0]
    scale = next((c for c in cards if c.get("type") == "scale"), None)
    if readiness.get("canonical_revenue_available") and producing and scale and \
            "No verified won-deal producers" in str(scale.get("headline")):
        out.append(f"overview:scale card says no producers over {len(producing)} "
                   "producing campaign row(s)")
    return out


def database_reachable() -> str | None:
    """None when the database answers a read; otherwise why not.

    ``init_pool()`` does not raise without ``DATABASE_URL`` (it disables
    writes), and every builder answers an unreachable database with an EMPTY
    payload — which passes every per-window check vacuously. Certifying that
    would certify nothing, so the windows are not audited at all."""
    try:
        from db.connection import get_conn  # noqa: PLC0415
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
    except MemoryError:
        raise
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"[:300]
    return None


def run(windows, *, now=None) -> tuple[Findings, dict]:
    f = Findings()
    check_retired_readers_absent(f)
    check_legacy_readers(f)
    check_account_scope(f)
    check_frontend_gates(f)
    why = database_reachable()
    if why is not None:
        f.unavailable_now("database", f"the database answered no read: {why}")
        return f, {w: {"window": w, "available": False} for w in windows}
    per_window = {w: audit_window(w, f, now=now) for w in windows}
    return f, per_window


def main() -> int:
    from analysis.business_windows import WINDOW_KEYS  # noqa: PLC0415
    parser = argparse.ArgumentParser(
        description="Read-only PR-ADS-177C Campaigns page certification gate")
    parser.add_argument("--window", default=None,
                        help="Audit ONE business window instead of all of them")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable output (exit code is unchanged)")
    args = parser.parse_args()
    if args.window and args.window not in WINDOW_KEYS:
        print(f"Unknown window {args.window!r}. Supported: {', '.join(WINDOW_KEYS)}",
              file=sys.stderr)
        return EXIT_UNAVAILABLE
    windows = (args.window,) if args.window else tuple(WINDOW_KEYS)
    try:
        from db.connection import init_pool  # noqa: PLC0415
        init_pool()
    except Exception as exc:  # noqa: BLE001
        payload = {"certified": False, "exit_code": EXIT_UNAVAILABLE,
                   "external_writes_performed": False,
                   "unavailable": [f"database pool could not be initialised: {exc}"]}
        print(json.dumps(payload, indent=2) if args.json
              else f"UNAVAILABLE — database pool could not be initialised: {exc}")
        return EXIT_UNAVAILABLE

    findings, per_window = run(windows)
    exit_code = findings.exit_code
    result = {
        "certified": exit_code == EXIT_CERTIFIED,
        "exit_code": exit_code,
        "external_writes_performed": False,
        "windows_audited": list(windows),
        "violations": findings.violations,
        "unavailable": findings.unavailable,
        "checks": findings.checks,
        "per_window": per_window,
    }
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print("=" * 78)
        print("  PR-ADS-177C — CAMPAIGNS PAGE CERTIFICATION (READ-ONLY)")
        print("=" * 78)
        for c in findings.checks:
            print(f"  {'✓' if c['ok'] else '✗'} {c['check']}"
                  + (f"\n      {c['detail']}" if c["detail"] else ""))
        verdict = {EXIT_CERTIFIED: "CERTIFIED", EXIT_VIOLATION: "VIOLATION",
                   EXIT_UNAVAILABLE: "UNAVAILABLE"}[exit_code]
        print(f"\n  {verdict} — exit {exit_code}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
