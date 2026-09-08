"""
analysis/sql_doctrine_registry.py

PR-ADS-158 — the REVIEWED registry of every production SQL consumer, and the
classification rules that bind each discovered code occurrence to it.

This file is data. It was produced by reading the code at the audited commit,
not by inferring from file names: ``canonical_contact_outcome_service`` is
registered as the LEGACY doctrine because its ``SQL_DEFINITION`` constant says
``"latest status_category = qualified"`` and its date field is
``contact_created_at`` (services/canonical_contact_outcome_service.py:64-66),
whatever its name suggests.

Three sections:

``CONSUMERS``        one record per production consumer (PR-ADS-158 §4 fields).
``RULES``            path (+ symbol, + pattern) → classification. The discovery
                     scanner in ``analysis/sql_doctrine_audit`` classifies every
                     production occurrence through these rules; an occurrence no
                     rule matches is ``unknown_requires_review`` and makes the
                     audit incomplete. Rules are therefore deliberately narrow:
                     a whole-file rule is used only where the whole file serves
                     one consumer.
``CPQL_CONSUMERS``   every CPQL field, with its numerator and denominator
                     contract (§8).
``DECISION_SURFACES`` every decision that depends on an SQL count (§8).
``KNOWN_CONTRACT_CONFLICTS`` the doctrine conflicts the audit documents rather
                     than fixes (§11).

Nothing here changes behaviour. Nothing here is imported by production code.
"""

from __future__ import annotations

from analysis.sql_doctrine_audit import (
    CLS_CANONICAL,
    CLS_DIAGNOSTIC,
    CLS_GOOGLE_ADS_CONVERSION,
    CLS_INACTIVE,
    CLS_LEGACY,
    CLS_MIXED,
)

# ── Shared definition strings (quoted from code, not paraphrased) ────────────
LEGACY_DEF = "latest status_category = qualified"
LEGACY_DATE = "contact_created_at"
LEGACY_DEDUP = "COALESCE(NULLIF(contact_id, ''), 'id:' || leads.id)"
LEGACY_TABLE = "leads"

LIFECYCLE_DEF = "entered lifecycle stage 'salesqualifiedlead' (hs_v2_date_entered_salesqualifiedlead)"
LIFECYCLE_DATE = "date_entered_sql"
LIFECYCLE_DEDUP = "contact_id"
LIFECYCLE_TABLE = "hubspot_contact_funnel"

SNAPSHOT_DEF = "campaigns.confirmed_sqls (count of mql_status in {CLOSED - Sales Qualified, CLOSED - Deal Created} from data/crm_contacts.json)"
SNAPSHOT_DATE = "run_date (scheduler snapshot date)"
SNAPSHOT_TABLE = "campaigns (scheduler snapshot)"

EVIDENCE = ["7d", "14d", "30d", "60d", "180d", "all_time"]
BUSINESS = ["current_quarter", "last_quarter", "last_6_months", "ytd", "all_time"]


def _c(consumer, purpose, *, endpoint, service_function, repository_source,
       source_table, sql_definition, date_field, dedup_key, windows, scope,
       classification, code_location, migration_notes, truth_status,
       confidence="confirmed", headline=False, row=False, cpql=False,
       filters=False, sorting=False, drawer=False, export=False, decision=False,
       executive=False, operational=False) -> dict:
    return {
        "consumer": consumer,
        "purpose": purpose,
        "api_endpoint": endpoint,
        "service_function": service_function,
        "repository_source": repository_source,
        "source_table": source_table,
        "sql_definition": sql_definition,
        "date_field": date_field,
        "dedup_key": dedup_key,
        "windows": windows,
        "scope": scope,
        "affects_headline": headline,
        "affects_row": row,
        "affects_cpql": cpql,
        "affects_filters": filters,
        "affects_sorting": sorting,
        "affects_drawer": drawer,
        "affects_export": export,
        "affects_decision": decision,
        "affects_executive_totals": executive,
        "affects_operational_decisions": operational,
        "truth_status_behaviour": truth_status,
        "code_location": code_location,
        "migration_notes": migration_notes,
        "confidence": confidence,
        "classification": classification,
    }


# ═════════════════════════════════════════════════════════════════════════════
# CONSUMERS
# ═════════════════════════════════════════════════════════════════════════════
CONSUMERS: list[dict] = [
    # ── Doctrine engines ────────────────────────────────────────────────────
    _c("Legacy contact-outcome contract (canonical_contact_outcome_service)",
       "The PR-ADS-152 reconciliation layer every legacy page attaches as "
       "sql_reconciliation. Despite its name it defines SQL as the legacy status.",
       endpoint="(library; surfaced by /api/campaigns, /api/keyword-evidence, "
                "/api/search-term-evidence, /api/dashboard/overview, "
                "/api/revenue-by-source, /api/revenue-performance, /api/leads)",
       service_function="services.canonical_contact_outcome_service.build / page_reconciliation",
       repository_source="db.canonical_contact_outcome_repository.fetch_canonical_inputs",
       source_table="leads + lead_truth_exclusions + contact_source_classification",
       sql_definition=LEGACY_DEF, date_field=LEGACY_DATE, dedup_key=LEGACY_DEDUP,
       windows=EVIDENCE + BUSINESS, scope="all_source|google_ads_source|campaign_attributable",
       classification=CLS_LEGACY,
       code_location="services/canonical_contact_outcome_service.py:_reconciliation_status",
       migration_notes="Reconciliation status counts stale/missing classification "
                       "over ALL in-window contacts, not only SQLs; a non-SQL gap "
                       "downgrades the SQL status (PR-ADS-158 §7). Every consumer "
                       "below that attaches sql_reconciliation inherits this.",
       truth_status="reconciled|partial|mismatch|unavailable; unavailable → all None",
       headline=True, decision=True, executive=True, operational=True),
    _c("Canonical CRM funnel contract (canonical_crm_funnel_service)",
       "The lifecycle SQL event population: stage-entry evidence on date_entered_sql.",
       endpoint="GET /api/crm-funnel, /api/crm-funnel/contacts, /api/crm-funnel/operational-status",
       service_function="services.canonical_crm_funnel_service.build / contacts / build_populations",
       repository_source="db.crm_funnel_repository.fetch_funnel_contacts (recovery COALESCE)",
       source_table=LIFECYCLE_TABLE + " + hubspot_lifecycle_stage_history",
       sql_definition=LIFECYCLE_DEF, date_field=LIFECYCLE_DATE, dedup_key=LIFECYCLE_DEDUP,
       windows=EVIDENCE + BUSINESS,
       scope="all_source|google_ads_source|campaign_attributable|keyword_attributable",
       classification=CLS_CANONICAL,
       code_location="services/canonical_crm_funnel_service.py:build_populations",
       migration_notes="Reference standard. Known gap: fetch_funnel_contact_page and "
                       "fetch_operational_status_counts filter the bare column without "
                       "the history-recovery COALESCE the headline read applies "
                       "(db/crm_funnel_repository.py). Funnel status turns partial on "
                       "ANY event's missing stage date, not only SQL.",
       truth_status="reconciled|partial|mismatch|unavailable; mismatch/unavailable → count None",
       headline=True, row=True, filters=True, sorting=True, drawer=True,
       executive=True),
    _c("Platform SQL attribution (keyword / search-term units)",
       "Shared attribution of legacy qualified paid-search contacts to keyword "
       "criteria and search-term units.",
       endpoint="GET /api/keyword-evidence*, /api/search-term-evidence*",
       service_function="services.platform_sql_attribution_service.fetch_and_resolve_contacts / attribute_keywords / attribute_search_terms",
       repository_source="db.platform_sql_attribution_repository.fetch_sql_contacts",
       source_table=LEGACY_TABLE,
       sql_definition="status_category = 'qualified' after DISTINCT ON latest snapshot "
                      "(SQL_DEFINITION = \"status_category qualified\")",
       date_field=LEGACY_DATE, dedup_key=LEGACY_DEDUP, windows=EVIDENCE,
       scope="keyword_attributable (unique criterion) | search-term (always unavailable: no persisted query)",
       classification=CLS_LEGACY,
       code_location="services/platform_sql_attribution_service.py:fetch_and_resolve_contacts",
       migration_notes="Keyword attribution is criterion-level (campaign_id + exact "
                       "normalized keyword); the lifecycle funnel's keyword scope is a "
                       "HubSpot label presence check — the two keyword_attributable "
                       "definitions are not the same population.",
       truth_status="attributed|known_zero|ambiguous|unavailable|mapping_review|partial_attribution; never a fabricated 0",
       row=True, filters=True, sorting=True, drawer=True, export=True, decision=True,
       operational=True),

    # ── Platform Evidence ───────────────────────────────────────────────────
    _c("Campaign Evidence table + KPI strip",
       "Per-campaign confirmed_sqls, confirmed_sqls_total, mapping coverage, CPQL, outcome status.",
       endpoint="GET /api/campaigns",
       service_function="services.campaign_evidence_service.build_campaign_evidence / _row / _build_summary",
       repository_source="db.revenue_repository.fetch_lead_quality",
       source_table=LEGACY_TABLE,
       sql_definition="status_category = 'qualified' (lq[_QUALIFIED]) on paid_search, "
                      "pseudo/email campaigns excluded, lead_truth_exclusions applied",
       date_field=LEGACY_DATE, dedup_key=LEGACY_DEDUP, windows=EVIDENCE + ["days=1..365"],
       scope="campaign_attributable (mapped) | unmatched | excluded_not_google | total_paid_search",
       classification=CLS_LEGACY,
       code_location="services/campaign_evidence_service.py:build_campaign_evidence",
       migration_notes="Attaches sql_reconciliation(SCOPE_CAMPAIGN_ATTRIBUTABLE, "
                       "consumer_count=mapped_sqls). Backend never withholds; the "
                       "frontend gate campaignSqlPublication() does. Production 30d: "
                       "legacy 6 vs lifecycle campaign-attributable 8.",
       truth_status="counts None when leads unavailable; aggregate/CPQL/filters/sorts gated in frontend on reconciled",
       headline=True, row=True, cpql=True, filters=True, sorting=True, drawer=True,
       decision=True, executive=True, operational=True),
    _c("Campaign drawer (headline, lead quality, countries, recent leads)",
       "Per-campaign drawer evidence on the same legacy population.",
       endpoint="GET /api/campaign-detail, GET /api/campaigns/{campaign_name}/detail",
       service_function="services.campaign_evidence_service.build_campaign_drawer_evidence; api.server._build_campaign_detail",
       repository_source="db.revenue_repository.fetch_campaign_lead_detail",
       source_table=LEGACY_TABLE, sql_definition="status_category == _QUALIFIED",
       date_field=LEGACY_DATE, dedup_key=LEGACY_DEDUP, windows=EVIDENCE,
       scope="campaign_attributable (one campaign)", classification=CLS_LEGACY,
       code_location="services/campaign_evidence_service.py:build_campaign_drawer_evidence",
       migration_notes="Embeds keyword preview (attributed_sqls via platform attribution) "
                       "and flagged-term preview. Frontend defect: static/app.js "
                       "renderCampaignDrawer reads drawerSqlPub before its const "
                       "declaration (temporal dead zone).",
       truth_status="db_unavailable on envelope; drawer KPIs withheld via campaignSqlPublication()",
       row=True, cpql=True, drawer=True, decision=True, operational=True),
    _c("Keyword Evidence table / drawer / CSV export",
       "attributed_sqls per keyword criterion; sql_state filter; attributed_sqls sort.",
       endpoint="GET /api/keyword-evidence, /api/keyword-evidence/detail, /api/keyword-evidence/export",
       service_function="services.keyword_evidence_service._keyword_sql_attribution / _apply_keyword_sql / build_keyword_drawer",
       repository_source="services.platform_sql_attribution_service (leads)",
       source_table=LEGACY_TABLE, sql_definition="status_category = 'qualified' (platform attribution)",
       date_field=LEGACY_DATE, dedup_key=LEGACY_DEDUP, windows=EVIDENCE,
       scope="keyword_attributable (unique criterion)", classification=CLS_LEGACY,
       code_location="services/keyword_evidence_service.py:_keyword_sql_attribution",
       migration_notes="Attaches sql_reconciliation(SCOPE_KEYWORD_ATTRIBUTABLE, consumer_count). "
                       "Frontend note kwSqlCoverageNote() discloses but gates nothing; "
                       "CSV export carries attributed_sqls + status columns.",
       truth_status="row states never fabricate 0; page discloses mismatch only",
       row=True, filters=True, sorting=True, drawer=True, export=True, operational=True),
    _c("Search Terms table / flagged tab / drawer / CSV export",
       "attributed_sqls per term unit (always unavailable: no persisted query), "
       "flagged-tab SQL evidence KPI and priority score.",
       endpoint="GET /api/search-term-evidence, /flagged, /term, /export",
       service_function="services.search_term_evidence_service._search_term_sql_attribution / _flagged_kpis / _flagged_priority",
       repository_source="services.platform_sql_attribution_service (leads)",
       source_table=LEGACY_TABLE, sql_definition="status_category = 'qualified' (platform attribution)",
       date_field=LEGACY_DATE, dedup_key=LEGACY_DEDUP, windows=EVIDENCE,
       scope="campaign_attributable (flagged tab) | search-term unit (unavailable)",
       classification=CLS_LEGACY,
       code_location="services/search_term_evidence_service.py:_search_term_sql_attribution",
       migration_notes="Reconciliation attached WITHOUT consumer_count. Flagged priority "
                       "adds +15 for proven_zero_qualified_outcome. UI label says "
                       "'lifecycle SQLs' on the flagged tab while the evidence is legacy.",
       truth_status="unavailable rows render '—'; flagged truth_state partial when attribution unavailable",
       headline=True, row=True, filters=True, sorting=True, drawer=True, export=True,
       decision=True, operational=True),
    _c("Geo Intelligence country summary (Countries page)",
       "confirmed_sqls per country from raw leads snapshots on run_date.",
       endpoint="GET /api/leads/country-summary",
       service_function="api.server.api_leads_country_summary",
       repository_source="inline SQL in api/server.py",
       source_table=LEGACY_TABLE,
       sql_definition="SUM(CASE WHEN status_category = 'qualified' THEN 1 ELSE 0 END)",
       date_field="run_date", dedup_key="CASE WHEN contact_id <> '' THEN contact_id ELSE CAST(id AS TEXT) END",
       windows=EVIDENCE, scope="all_source (no paid_search / exclusion filters)",
       classification=CLS_LEGACY,
       code_location="api/server.py:api_leads_country_summary",
       migration_notes="Different window field (run_date) and different filters from "
                       "Campaign Evidence under the same Evidence Window label. No "
                       "sql_reconciliation. Frontend loadGeo coerces missing rows to 0.",
       truth_status="none — no reconciliation block; frontend renders 0 for absent rows",
       headline=True, row=True, filters=True, sorting=True, decision=True, operational=True),

    # ── Executive / Revenue ─────────────────────────────────────────────────
    _c("Dashboard Overview KPI strip (google_ads_source_sqls + kpis.sqls)",
       "Headline Google Ads-source SQLs (legacy contract) and campaign-attributable "
       "kpis.sqls from the Revenue Decision Mart.",
       endpoint="GET /api/dashboard/overview",
       service_function="services.dashboard_overview_service.build_dashboard_overview",
       repository_source="canonical_contact_outcome_service.page_reconciliation + revenue_decision_mart.summary",
       source_table=LEGACY_TABLE, sql_definition=LEGACY_DEF, date_field=LEGACY_DATE,
       dedup_key=LEGACY_DEDUP, windows=BUSINESS,
       scope="google_ads_source (headline) | campaign_attributable (kpis.sqls)",
       classification=CLS_MIXED,
       code_location="services/dashboard_overview_service.py:build_dashboard_overview",
       migration_notes="Same page publishes legacy SQL KPIs beside the canonical "
                       "lifecycle_sqls funnel (see next record). Decision cards and "
                       "waste signals read legacy row sqls.",
       truth_status="headline None on mismatch; kpis.sqls None when mart withholds",
       headline=True, decision=True, executive=True, operational=True),
    _c("Dashboard Overview lifecycle activity / cohort",
       "Lead-anchored lifecycle cohort and lifecycle_sqls from the canonical funnel.",
       endpoint="GET /api/dashboard/overview",
       service_function="services.dashboard_overview_service._lifecycle_funnel_block / _lifecycle_previous_period",
       repository_source="services.canonical_crm_funnel_service.build",
       source_table=LIFECYCLE_TABLE, sql_definition=LIFECYCLE_DEF, date_field=LIFECYCLE_DATE,
       dedup_key=LIFECYCLE_DEDUP, windows=BUSINESS, scope="all_source",
       classification=CLS_CANONICAL,
       code_location="services/dashboard_overview_service.py:_lifecycle_funnel_block",
       migration_notes="Only executive surface already on the lifecycle doctrine.",
       truth_status="counts None when unavailable or mismatch",
       headline=True, row=True, executive=True),
    _c("Revenue Decision Mart summary.sqls / row sqls",
       "Shared top-line SQL count consumed by Dashboard Revenue, Campaigns, Countries, Deals, ROAS pages.",
       endpoint="GET /api/revenue-performance (mart views)",
       service_function="services.revenue_decision_mart.build_revenue_decision_mart / _summary_block",
       repository_source="services.revenue_attribution_service._build_db_rows → db.revenue_repository.fetch_lead_quality",
       source_table=LEGACY_TABLE, sql_definition="row.get('status_category') == 'qualified'",
       date_field=LEGACY_DATE, dedup_key=LEGACY_DEDUP, windows=BUSINESS,
       scope="campaign_attributable", classification=CLS_LEGACY,
       code_location="services/revenue_decision_mart.py:_summary_block",
       migration_notes="Attaches sql_reconciliation(SCOPE_CAMPAIGN_ATTRIBUTABLE, consumer_count). "
                       "Cross-page parity asserts this count equal across five pages.",
       truth_status="withheld wholesale via lead_metrics_withheld (None, never 0)",
       headline=True, row=True, sorting=True, decision=True, executive=True, operational=True),
    _c("Revenue attribution service (ROAS by Campaign / Country, verdicts)",
       "Row-level sqls and classify_verdict (watch/waste) on the legacy population; "
       "JSON fallback path imports analysis.core.QUALIFIED.",
       endpoint="GET /api/revenue-attribution, /api/reports/roas/*",
       service_function="services.revenue_attribution_service._build_db_rows / _finalize_row / classify_verdict",
       repository_source="db.revenue_repository.fetch_lead_quality",
       source_table=LEGACY_TABLE, sql_definition="row.get('status_category') == 'qualified'",
       date_field=LEGACY_DATE, dedup_key=LEGACY_DEDUP, windows=BUSINESS,
       scope="campaign_attributable | country", classification=CLS_MIXED,
       code_location="services/revenue_attribution_service.py:_build_db_rows",
       migration_notes="classify_verdict substitutes 0 for a withheld SQL count "
                       "(sqls_for_verdict). _build_from_json (mql_status in QUALIFIED) "
                       "is reachable only on the DB-unavailable fallback.",
       truth_status="sqls None when lead metrics withheld; verdict uses 0 in that case",
       row=True, sorting=True, decision=True, executive=True, operational=True),
    _c("Revenue by Source (source_attribution_service)",
       "Group/channel/platform sqls from contact_source_classification, with the "
       "Google Ads group overridden by the legacy contract's google_ads_source count.",
       endpoint="GET /api/revenue-by-source, /api/revenue-performance/source-platform-detail",
       service_function="services.source_attribution_service.build_revenue_by_source / build_source_platform_detail",
       repository_source="db.revenue_repository.fetch_source_leads / fetch_source_contact_details",
       source_table="contact_source_classification (+ leads)",
       sql_definition="row.get('status_category') == 'qualified' (no dedup, no exclusions) "
                      "→ Google Ads group replaced by page_reconciliation google_ads_source_sqls",
       date_field=LEGACY_DATE, dedup_key="none (classification row) / contract dedup for Google Ads group",
       windows=BUSINESS, scope="acquisition group | google_ads_source", classification=CLS_MIXED,
       code_location="services/source_attribution_service.py:build_revenue_by_source",
       migration_notes="Keeps the raw classification count when the contract is unavailable "
                       "(explicit legacy fallback).",
       truth_status="mismatch → None; unavailable → raw legacy count retained",
       headline=True, row=True, drawer=True, decision=True, executive=True, operational=True),
    _c("Dashboard Channels", "Channel/platform sqls rollup, total_sqls KPI, SQL trend.",
       endpoint="GET /api/dashboard/channels",
       service_function="services.dashboard_channels_service._build_channels_and_platforms / _build_kpis / _build_trend",
       repository_source="source_attribution_service groups + db.revenue_repository.fetch_source_leads_daily",
       source_table="contact_source_classification",
       sql_definition="status_category == 'qualified' (trend: != 'qualified' → skip)",
       date_field=LEGACY_DATE, dedup_key="none (classification row)", windows=BUSINESS,
       scope="source group", classification=CLS_MIXED,
       code_location="services/dashboard_channels_service.py:_build_kpis",
       migration_notes="total_sqls = sum(c.get('sqls') or 0) renders a withheld channel as 0 "
                       "(Campaigns/Countries use None-if-any-None).",
       truth_status="KPI sums None as 0 — outlier",
       headline=True, row=True, sorting=True, decision=True, executive=True),
    _c("Dashboard Campaigns", "Campaign rows sqls, kpis.sqls, SQL producer status, period delta.",
       endpoint="GET /api/dashboard/campaigns",
       service_function="services.dashboard_campaigns_service._build_campaign_rows / _campaign_status / _build_kpis",
       repository_source="revenue_decision_mart rows", source_table=LEGACY_TABLE,
       sql_definition=LEGACY_DEF, date_field=LEGACY_DATE, dedup_key=LEGACY_DEDUP,
       windows=BUSINESS, scope="campaign_attributable", classification=CLS_LEGACY,
       code_location="services/dashboard_campaigns_service.py:_campaign_status",
       migration_notes="Status vocabulary 'Spend without SQL / customer proof' differs from Campaign Evidence.",
       truth_status="kpis.sqls None if any Google row withholds",
       headline=True, row=True, drawer=True, decision=True, executive=True, operational=True),
    _c("Dashboard Countries", "Country rows sqls, residual sqls, SQL producer status, regional mix.",
       endpoint="GET /api/dashboard/countries",
       service_function="services.dashboard_countries_service._build_country_rows / _country_status / _build_residual",
       repository_source="revenue_decision_mart country rows", source_table=LEGACY_TABLE,
       sql_definition=LEGACY_DEF, date_field=LEGACY_DATE, dedup_key=LEGACY_DEDUP,
       windows=BUSINESS, scope="country_attributed_sqls (campaign population narrowed to canonical country)",
       classification=CLS_LEGACY,
       code_location="services/dashboard_countries_service.py:_country_status",
       migration_notes="Residual SQLs derived by subtraction from summary.sqls.",
       truth_status="kpis.sqls None if any row withholds",
       headline=True, row=True, drawer=True, decision=True, executive=True, operational=True),
    _c("Dashboard Revenue", "kpis.sqls, sql_to_customer_rate, revenue_per_sql, SQL trend series.",
       endpoint="GET /api/dashboard/revenue",
       service_function="services.dashboard_revenue_service._build_kpis / _build_customer_trend",
       repository_source="revenue_decision_mart summary + db.revenue_repository.fetch_lead_daily_series",
       source_table=LEGACY_TABLE,
       sql_definition="SUM(CASE WHEN status_category = 'qualified' THEN 1 ELSE 0 END) (series)",
       date_field=LEGACY_DATE, dedup_key=LEGACY_DEDUP, windows=BUSINESS,
       scope="campaign_attributable", classification=CLS_LEGACY,
       code_location="services/dashboard_revenue_service.py:_build_kpis",
       migration_notes="revenue_per_sql is an inverse-CPQL family metric on the legacy denominator.",
       truth_status="rates None on None/0 denominator",
       headline=True, row=True, executive=True),
    _c("Dashboard Deals", "kpis.sqls, sqls_not_closed_won panel, source breakdown status.",
       endpoint="GET /api/dashboard/deals",
       service_function="services.dashboard_deals_service._build_kpis / _build_sql_no_deal / _source_status",
       repository_source="revenue_decision_mart summary + db.revenue_repository.fetch_sql_lead_details",
       source_table=LEGACY_TABLE, sql_definition="WHERE d.status_category = 'qualified'",
       date_field=LEGACY_DATE, dedup_key=LEGACY_DEDUP, windows=BUSINESS,
       scope="campaign_attributable | source group", classification=CLS_LEGACY,
       code_location="services/dashboard_deals_service.py:_build_sql_no_deal",
       migration_notes="Per-contact SQL rows (contact id + company) in the drawer.",
       truth_status="sqls_not_closed_won None when unavailable",
       headline=True, row=True, drawer=True, decision=True, executive=True, operational=True),
    _c("Legacy summary KPIs (/api/summary)",
       "confirmed_sqls and avg_cpql_usd from the campaigns scheduler snapshot.",
       endpoint="GET /api/summary", service_function="api.server.api_summary",
       repository_source="inline SQL over campaigns JOIN runs (latest run)",
       source_table=SNAPSHOT_TABLE, sql_definition=SNAPSHOT_DEF, date_field=SNAPSHOT_DATE,
       dedup_key="none (latest run id)", windows=["days lookback"], scope="other (snapshot)",
       classification=CLS_LEGACY, code_location="api/server.py:api_summary",
       migration_notes="Third doctrine: mql_status literals counted from JSON, no dedup, "
                       "no exclusions, run_date. Not registered in cross-page parity.",
       truth_status="db_unavailable → None; otherwise raw snapshot",
       headline=True, cpql=True, executive=True),
    _c("Dashboard trends + trend alerts (/api/dashboard/trends)",
       "Period-over-period confirmed_sqls deltas and 'spend rose without SQLs' alerts.",
       endpoint="GET /api/dashboard/trends",
       service_function="api.server.api_dashboard_trends / _build_trend_alerts / _compute_severity",
       repository_source="inline SQL over campaigns snapshot", source_table=SNAPSHOT_TABLE,
       sql_definition=SNAPSHOT_DEF, date_field=SNAPSHOT_DATE, dedup_key="DISTINCT ON (campaign_name) latest run_date",
       windows=["days lookback"], scope="other (snapshot)", classification=CLS_LEGACY,
       code_location="api/server.py:api_dashboard_trends",
       migration_notes="Severity +30 when confirmed_sqls == 0 with spend.",
       truth_status="insufficient_data rather than comparing to zero",
       headline=True, row=True, sorting=True, decision=True, executive=True, operational=True),
    _c("Historical Intelligence (/api/historical-intelligence)",
       "30d-vs-30d confirmed_sqls and CPQL movement with improving/deteriorating verdicts.",
       endpoint="GET /api/historical-intelligence",
       service_function="analysis.historical_intelligence.compute_campaign_trends / _safe_cpql / _classify_overall_trend",
       repository_source="analysis.historical_intelligence.load_campaign_trend_rows (campaigns snapshot)",
       source_table=SNAPSHOT_TABLE, sql_definition="COALESCE(confirmed_sqls, 0) from campaigns",
       date_field=SNAPSHOT_DATE, dedup_key="none", windows=["30d vs prior 30d"],
       scope="other (snapshot)", classification=CLS_LEGACY,
       code_location="analysis/historical_intelligence.py:_safe_cpql",
       migration_notes="COALESCE(confirmed_sqls, 0) turns a missing value into a zero. "
                       "The report block is never wired into the emailed report.",
       truth_status="None-to-0 coercion in the loader",
       row=True, cpql=True, decision=True, operational=True),

    # ── Action queue / operational ──────────────────────────────────────────
    _c("Action Queue — campaign items", "Queue inclusion/scoring on snapshot confirmed_sqls and FIX/CUT verdicts.",
       endpoint="GET /api/action-queue", service_function="api.server._build_campaign_queue_items",
       repository_source="inline SQL over campaigns snapshot", source_table=SNAPSHOT_TABLE,
       sql_definition=SNAPSHOT_DEF, date_field=SNAPSHOT_DATE, dedup_key="DISTINCT ON (campaign_name)",
       windows=["days lookback"], scope="other (snapshot)", classification=CLS_LEGACY,
       code_location="api/server.py:_build_campaign_queue_items",
       migration_notes="qualifies when sqls == 0 and spend > 0; score +30.",
       truth_status="db_unavailable → empty queue with flag",
       row=True, sorting=True, decision=True, operational=True),
    _c("Action Queue — geo items", "Country queue items on raw leads confirmed_sqls (run_date).",
       endpoint="GET /api/action-queue", service_function="api.server._build_geo_queue_items",
       repository_source="inline SQL over leads + geo", source_table=LEGACY_TABLE,
       sql_definition="SUM(CASE WHEN status_category = 'qualified' THEN 1 ELSE 0 END)",
       date_field="run_date", dedup_key="CASE WHEN contact_id <> '' THEN contact_id ELSE CAST(id AS TEXT) END",
       windows=["days lookback"], scope="all_source (country)", classification=CLS_LEGACY,
       code_location="api/server.py:_build_geo_queue_items",
       migration_notes="Same run_date population as the Geo page; not the Campaign Evidence population.",
       truth_status="db_unavailable → empty", row=True, decision=True, operational=True),
    _c("Lead Quality API (/api/leads)", "Legacy status buckets on run_date; no frontend caller.",
       endpoint="GET /api/leads", service_function="api.server.api_leads / _lead_quality_sql_reconciliation",
       repository_source="inline SQL over leads", source_table=LEGACY_TABLE,
       sql_definition="status_category grouped (qualified bucket)", date_field="run_date",
       dedup_key="CASE WHEN contact_id <> '' THEN contact_id ELSE CAST(id AS TEXT) END",
       windows=EVIDENCE, scope="all_source", classification=CLS_LEGACY,
       code_location="api/server.py:api_leads",
       migration_notes="Attaches page_reconciliation(all_source) with page_date_field=run_date. "
                       "The Leads page UI reads /api/crm-funnel instead; this route has no UI consumer.",
       truth_status="sql_reconciliation attached; counts raw",
       headline=True, row=True, operational=True),
    _c("Campaign identity workbench", "Per-label sqls in the mapping review workbench.",
       endpoint="GET /api/campaign-mapping-review",
       service_function="services.campaign_identity_service (sqls tally)",
       repository_source="lead rows passed by caller (fetch_lead_quality)", source_table=LEGACY_TABLE,
       sql_definition="r.get('status_category') == 'qualified'", date_field=LEGACY_DATE,
       dedup_key=LEGACY_DEDUP, windows=EVIDENCE, scope="per label", classification=CLS_LEGACY,
       code_location="services/campaign_identity_service.py:build_campaign_mapping_review",
       migration_notes="Decision aid for mapping; SQL count informs which labels matter.",
       truth_status="raw", row=True, decision=True, operational=True),
    _c("Mailchimp attribution audit", "Legacy qualified contact ids for Mailchimp overlap.",
       endpoint="GET /api/mailchimp/audit",
       service_function="services.mailchimp_audit_service (sql_contacts)",
       repository_source="db.mailchimp_repository.fetch_durable_outcome_populations",
       source_table=LEGACY_TABLE, sql_definition="WHERE status_category = 'qualified' AND contact_id IS NOT NULL",
       date_field=LEGACY_DATE, dedup_key=LEGACY_DEDUP, windows=BUSINESS, scope="all_source (with HubSpot id)",
       classification=CLS_LEGACY, code_location="db/mailchimp_repository.py:fetch_durable_outcome_populations",
       migration_notes="Audit-only surface but published as a count.",
       truth_status="None when unavailable", headline=True, operational=True),
    _c("GCLID attribution rows", "status_category pass-through on gclid_attribution rows.",
       endpoint="GET /api/gclid-attribution", service_function="api.server.api_gclid_attribution; db.writers.write_gclid_attribution",
       repository_source="gclid_attribution table", source_table="gclid_attribution",
       sql_definition="_map_status_category(mql_status) stored per row (no count)",
       date_field=LEGACY_DATE, dedup_key="attribution_key", windows=EVIDENCE, scope="per contact",
       classification=CLS_LEGACY, code_location="db/writers.py:write_gclid_attribution",
       migration_notes="Row-level status only; no SQL total published.",
       truth_status="raw", row=True, drawer=True, export=True),

    # ── Scheduled outputs ───────────────────────────────────────────────────
    _c("Weekly / monthly report pipeline (analysis.core → rule_advisor → email)",
       "Campaign truth confirmed_sqls, CPQL and FIX/HOLD/SCALE/CUT verdicts from a local "
       "JSON contact pull, emailed via SendGrid and served at /reports/latest.",
       endpoint="APScheduler weekly/monthly; POST /run/weekly, /run/monthly; GET /reports/latest",
       service_function="analysis.core.run_lead_quality / run_campaign_truth / determine_verdict; analysis.rule_advisor.generate_deterministic_report",
       repository_source="data/crm_contacts.json + outputs/*.json (files)", source_table="none (JSON)",
       sql_definition="mql_status in ['CLOSED - Sales Qualified', 'CLOSED - Deal Created'] (QUALIFIED)",
       date_field="none (grace_days affects junk only)", dedup_key="none",
       windows=["30d spend pull; contacts undated"], scope="other (per campaign label)",
       classification=CLS_LEGACY, code_location="analysis/core.py:determine_verdict",
       migration_notes="Third doctrine, undeduplicated and undated. SCALE requires "
                       "min_confirmed_sqls_30d; FIX on 0 SQLs with spend > 200. CPQL "
                       "rendered in the emailed table. Nothing reconciles the email "
                       "against any page.",
       truth_status="missing JSON → prose, CPQL 'N/A' on 0 SQLs",
       headline=True, row=True, cpql=True, export=True, decision=True, executive=True,
       operational=True),
    _c("campaigns snapshot writer (db.writers.write_campaigns)",
       "Persists confirmed_sqls and cpql_usd from campaign truth on run_date.",
       endpoint="scheduler.weekly / scheduler.monthly",
       service_function="db.writers.write_campaigns", repository_source="INSERT INTO campaigns",
       source_table=SNAPSHOT_TABLE, sql_definition="copies confirmed_sqls; cpql = spend / sqls",
       date_field=SNAPSHOT_DATE, dedup_key="none", windows=["snapshot"], scope="other",
       classification=CLS_LEGACY, code_location="db/writers.py:write_campaigns",
       migration_notes="Feeds /api/summary, /api/dashboard/trends, action queue, historical intelligence.",
       truth_status="cpql None when sqls == 0", row=True, cpql=True),
    _c("leads snapshot writer (db.writers.write_leads / _map_status_category)",
       "Derives status_category from mql_status and stores contact_created_at from createdate.",
       endpoint="scheduler.weekly / monthly / incremental_sync; scripts.backfill_hubspot",
       service_function="db.writers.write_leads / _map_status_category",
       repository_source="INSERT INTO leads", source_table=LEGACY_TABLE,
       sql_definition="mql_status in _QUALIFIED → 'qualified'", date_field=LEGACY_DATE,
       dedup_key="contact_id stored raw (dedup at read time)", windows=["sync range"],
       scope="all_source", classification=CLS_LEGACY,
       code_location="db/writers.py:_map_status_category",
       migration_notes="The upstream of every leads-based legacy consumer. Also writes "
                       "gclid_attribution and contact_source_classification status_category.",
       truth_status="n/a (writer)"),
    _c("Source classification cache writer", "status_category stored on contact_source_classification.",
       endpoint="scheduler.incremental_sync._sync_source_classification; POST /api/audit/sql-truth/repair-classification",
       service_function="services.source_attribution_service (classify) / services.canonical_classification_repair_service.run_repair",
       repository_source="db.writers.upsert_contact_source_classification",
       source_table="contact_source_classification", sql_definition="_map_status_category(mql_status)",
       date_field=LEGACY_DATE, dedup_key="contact_key", windows=["sync range"], scope="all_source",
       classification=CLS_LEGACY, code_location="db/writers.py:upsert_contact_source_classification",
       migration_notes="The cache whose stale/missing state drives the hidden partial status (§7).",
       truth_status="n/a (writer)"),
    _c("HubSpot contact funnel sync (canonical ingestion)",
       "Writes hubspot_contact_funnel.date_entered_sql from hs_v2_date_entered_salesqualifiedlead.",
       endpoint="scheduler.incremental_sync._sync_contact_funnel; POST /api/crm-funnel/sync",
       service_function="services.hubspot_contact_funnel_sync_service.run_contact_funnel_sync; connectors.hubspot_pull.normalize_contact_funnel_row",
       repository_source="db.writers.upsert_hubspot_contact_funnel", source_table=LIFECYCLE_TABLE,
       sql_definition=LIFECYCLE_DEF, date_field=LIFECYCLE_DATE, dedup_key=LIFECYCLE_DEDUP,
       windows=["watermarked sync"], scope="all_source", classification=CLS_CANONICAL,
       code_location="connectors/hubspot_pull.py:normalize_contact_funnel_row",
       migration_notes="createdate is never substituted for a missing stage date.",
       truth_status="n/a (writer)"),
    _c("Lifecycle stage-history recovery (CLI)",
       "Recovers missing stage-entry dates from lifecyclestage property history into hubspot_lifecycle_stage_history.",
       endpoint="python -m scripts.backfill_lifecycle_stage_history [--apply]",
       service_function="services.lifecycle_history_recovery_service",
       repository_source="db.crm_funnel_repository.fetch_contacts_missing_stage_dates / save_lifecycle_recovery_state",
       source_table="hubspot_lifecycle_stage_history", sql_definition=LIFECYCLE_DEF,
       date_field=LIFECYCLE_DATE, dedup_key=LIFECYCLE_DEDUP, windows=["all"], scope="all_source",
       classification=CLS_CANONICAL, code_location="services/lifecycle_history_recovery_service.py:run_recovery",
       migration_notes="Not scheduled; dry-run default. The coverage gap (SQL reached without "
                       "timestamp) is only reduced by running this.",
       truth_status="n/a (writer, CLI)", confidence="confirmed"),
    _c("Lead reconciliation (legacy business-date backfill)",
       "Backfills leads.contact_created_at from HubSpot createdate; excludes unresolvable rows.",
       endpoint="POST /api/lead-reconciliation/run",
       service_function="services.lead_reconciliation_service.run_lead_reconciliation",
       repository_source="db.writers.backfill_event_date_for_contact", source_table=LEGACY_TABLE,
       sql_definition="n/a (supplies the legacy date field, no SQL count)", date_field=LEGACY_DATE,
       dedup_key=LEGACY_DEDUP, windows=["all"], scope="all_source", classification=CLS_MIXED,
       code_location="services/lead_reconciliation_service.py:run_lead_reconciliation",
       migration_notes="Adapter that keeps the legacy date field complete; irrelevant once "
                       "consumers move to date_entered_sql.",
       truth_status="n/a"),

    # ── Diagnostics ─────────────────────────────────────────────────────────
    _c("SQL-truth audit (/api/audit/sql-truth)", "Compares three legacy scopes of the same population.",
       endpoint="GET /api/audit/sql-truth", service_function="services.sql_truth_audit_service.run",
       repository_source="db.canonical_contact_outcome_repository", source_table=LEGACY_TABLE,
       sql_definition=LEGACY_DEF, date_field=LEGACY_DATE, dedup_key=LEGACY_DEDUP,
       windows=BUSINESS + EVIDENCE, scope="all|google_ads_source|campaign|keyword",
       classification=CLS_DIAGNOSTIC, code_location="services/sql_truth_audit_service.py:build_audit",
       migration_notes="Treats the legacy doctrine as truth; never crosses doctrines.",
       truth_status="admin-only; contact id + company only"),
    _c("CRM funnel reconciliation (/api/crm-funnel/audit)",
       "Contact-by-contact legacy vs lifecycle comparison with date-shift vs population split.",
       endpoint="GET /api/crm-funnel/audit", service_function="services.crm_funnel_reconciliation_service.run / compare_sql_counts",
       repository_source="db.crm_funnel_repository.fetch_all_funnel_contacts / fetch_legacy_outcome_rows",
       source_table=LIFECYCLE_TABLE + " + leads", sql_definition="both doctrines side by side",
       date_field="contact_created_at vs date_entered_sql", dedup_key="contact_id", windows=BUSINESS,
       scope="all_source (+ lifecycle scope coverage)", classification=CLS_DIAGNOSTIC,
       code_location="services/crm_funnel_reconciliation_service.py:compare_sql_counts",
       migration_notes="Legacy side reads fetch_legacy_outcome_rows, not the Campaign Evidence query.",
       truth_status="admin-only"),
    _c("Cross-page canonical parity", "Asserts campaign_attributable_sqls equal across five legacy pages; lifecycle kept distinct by design.",
       endpoint="python -m scripts.audit_cross_page_canonical_parity",
       service_function="services.cross_page_parity_service", repository_source="service payloads",
       source_table="n/a", sql_definition="metric identities", date_field="per identity", dedup_key="n/a",
       windows=BUSINESS, scope="per identity", classification=CLS_DIAGNOSTIC,
       code_location="services/cross_page_parity_service.py:METRIC_IDENTITIES",
       migration_notes="/api/summary, trends and historical intelligence are not registered identities.",
       truth_status="n/a"),
    _c("Campaign Evidence certification gate", "PR-ADS-157 read-only certification of the campaign page.",
       endpoint="python -m scripts.audit_campaign_evidence_certification",
       service_function="scripts.audit_campaign_evidence_certification.run", repository_source="service payloads",
       source_table="n/a", sql_definition="asserts population reconciliation", date_field="n/a", dedup_key="n/a",
       windows=EVIDENCE, scope="campaign_attributable", classification=CLS_DIAGNOSTIC,
       code_location="scripts/audit_campaign_evidence_certification.py:run",
       migration_notes="n/a", truth_status="n/a"),
    _c("Search-term waste truth audit (CLI)", "Prints flagged-tab SQL evidence with a 'lifecycle SQLs' label.",
       endpoint="python -m scripts.audit_search_term_waste_truth",
       service_function="scripts.audit_search_term_waste_truth", repository_source="search_term_evidence_service payload",
       source_table=LEGACY_TABLE, sql_definition="status_category = 'qualified' (via platform attribution)",
       date_field=LEGACY_DATE, dedup_key=LEGACY_DEDUP, windows=EVIDENCE, scope="campaign_attributable",
       classification=CLS_MIXED, code_location="scripts/audit_search_term_waste_truth.py:main",
       migration_notes="Label says lifecycle; evidence is legacy.", truth_status="n/a"),
    _c("Google Ads platform conversions (keyword / search-term / campaign snapshot / daily delta)",
       "Platform conversion metrics rendered beside SQL numbers; never an SQL.",
       endpoint="GET /api/keyword-evidence*, /api/search-term-evidence*, /api/keywords, scheduler.daily.check_crm_delta",
       service_function="services.keyword_evidence_service._conversion_evidence; scheduler.daily.check_crm_delta",
       repository_source="db.keyword_repository / db.search_term_repository (conversions)",
       source_table="keyword_daily_facts / search_terms / campaigns.conversions",
       sql_definition="NOT an SQL — Google Ads conversion events", date_field="source_date",
       dedup_key="criterion / unit", windows=EVIDENCE, scope="platform", classification=CLS_GOOGLE_ADS_CONVERSION,
       code_location="services/keyword_evidence_service.py:_conversion_evidence",
       migration_notes="Geo page renders conversions as 'Conv.' three columns from 'SQLs' with no qualifier.",
       truth_status="disclosed as platform event on keyword/search-term drawers"),
    _c("Legacy JSON revenue fallback (revenue_attribution_service._build_from_json)",
       "mql_status-literal SQL counts from local JSON when the database is unavailable.",
       endpoint="GET /api/revenue-attribution (DB-unavailable fallback only)",
       service_function="services.revenue_attribution_service._build_from_json / _build_campaign_rows / _build_summary",
       repository_source="local JSON files", source_table="none (JSON)",
       sql_definition="props.get('mql_status') in QUALIFIED", date_field="none", dedup_key="none",
       windows=BUSINESS, scope="other", classification=CLS_INACTIVE,
       code_location="services/revenue_attribution_service.py:_build_from_json",
       migration_notes="Reachable only when the durable DB read fails; revenue withheld on that path.",
       truth_status="diagnostic fallback mode disclosed in source_health", confidence="confirmed"),
    _c("Claude advisor report writer (ADVISOR_MODE=claude)", "Optional LLM report rendering the same JSON SQL fields.",
       endpoint="ADVISOR_MODE=claude", service_function="analysis.advisor.generate_weekly_report",
       repository_source="outputs/*.json", source_table="none (JSON)", sql_definition="lead_quality qualified field",
       date_field="none", dedup_key="none", windows=["report"], scope="other", classification=CLS_INACTIVE,
       code_location="analysis/advisor.py:generate_weekly_report",
       migration_notes="Default mode is deterministic.", truth_status="n/a"),
]


# ═════════════════════════════════════════════════════════════════════════════
# RULES — every production occurrence must match one EXPLICIT binding.
#
# Binding contract (enforced by ``analysis.sql_doctrine_audit.validate_rules``
# and by the regression tests):
#   * ``path`` is an exact file. No folder or glob bindings exist.
#   * a rule names the enclosing ``symbol``(s) it covers, or a pattern from
#     ``SPECIFIC_PATTERNS``. Broad patterns (``sqls``, ``contact_created_at``,
#     ``cpql``, bare "SQLs" labels, ``confirmed_sqls`` …) can only be bound
#     together with a symbol.
#   * ``<module>`` bindings name the exact patterns they cover, so a new
#     module-level statement of another kind stays unreviewed.
#   * a new function inside ANY known file — engine, page service, scheduler,
#     frontend — therefore surfaces as ``unknown_requires_review`` and fails the
#     audit until it is reviewed and bound here.
#
# Whole-file exceptions: none. The only single-purpose modules (schema DDL,
# the lifecycle taxonomy, index.html) are bound as ``<module>`` + patterns,
# which is the same guarantee expressed explicitly.
# ═════════════════════════════════════════════════════════════════════════════
def _r(rid, path, classification, consumer, *, symbol=None, pattern=None) -> dict:
    rule = {"id": rid, "path": path, "classification": classification, "consumer": consumer}
    if symbol is not None:
        rule["symbol"] = list(symbol) if isinstance(symbol, (list, tuple, set)) else [symbol]
    if pattern is not None:
        rule["pattern"] = list(pattern) if isinstance(pattern, (list, tuple, set)) else [pattern]
    return rule


def _m(rid, path, classification, consumer, patterns) -> dict:
    """A module-level binding: ``<module>`` + the exact patterns it covers."""
    return _r(rid, path, classification, consumer, symbol="<module>", pattern=patterns)


_CE = "Campaign Evidence table + KPI strip"
_CD = "Campaign drawer (headline, lead quality, countries, recent leads)"
_KW = "Keyword Evidence table / drawer / CSV export"
_ST = "Search Terms table / flagged tab / drawer / CSV export"
_GEO = "Geo Intelligence country summary (Countries page)"
_OVW = "Dashboard Overview KPI strip (google_ads_source_sqls + kpis.sqls)"
_OVW_LC = "Dashboard Overview lifecycle activity / cohort"
_MART = "Revenue Decision Mart summary.sqls / row sqls"
_RAS = "Revenue attribution service (ROAS by Campaign / Country, verdicts)"
_SRC = "Revenue by Source (source_attribution_service)"
_CHAN = "Dashboard Channels"
_CAMP = "Dashboard Campaigns"
_CTRY = "Dashboard Countries"
_REV = "Dashboard Revenue"
_DEALS = "Dashboard Deals"
_SUMMARY = "Legacy summary KPIs (/api/summary)"
_TRENDS = "Dashboard trends + trend alerts (/api/dashboard/trends)"
_HI = "Historical Intelligence (/api/historical-intelligence)"
_AQC = "Action Queue — campaign items"
_AQG = "Action Queue — geo items"
_LEADS_API = "Lead Quality API (/api/leads)"
_WORKBENCH = "Campaign identity workbench"
_MAILCHIMP = "Mailchimp attribution audit"
_GCLID = "GCLID attribution rows"
_REPORT = "Weekly / monthly report pipeline (analysis.core → rule_advisor → email)"
_WRITE_CAMP = "campaigns snapshot writer (db.writers.write_campaigns)"
_WRITE_LEADS = "leads snapshot writer (db.writers.write_leads / _map_status_category)"
_WRITE_CLASS = "Source classification cache writer"
_SYNC = "HubSpot contact funnel sync (canonical ingestion)"
_RECOVERY = "Lifecycle stage-history recovery (CLI)"
_LEADREC = "Lead reconciliation (legacy business-date backfill)"
_LEGACY_ENGINE = "Legacy contact-outcome contract (canonical_contact_outcome_service)"
_FUNNEL_ENGINE = "Canonical CRM funnel contract (canonical_crm_funnel_service)"
_PLATFORM = "Platform SQL attribution (keyword / search-term units)"
_SQLTRUTH = "SQL-truth audit (/api/audit/sql-truth)"
_FUNNELREC = "CRM funnel reconciliation (/api/crm-funnel/audit)"
_PARITY = "Cross-page canonical parity"
_CERT = "Campaign Evidence certification gate"
_STWASTE = "Search-term waste truth audit (CLI)"
_GACONV = "Google Ads platform conversions (keyword / search-term / campaign snapshot / daily delta)"
_JSONFALLBACK = "Legacy JSON revenue fallback (revenue_attribution_service._build_from_json)"
_CLAUDE = "Claude advisor report writer (ADVISOR_MODE=claude)"
_AUDIT = "PR-ADS-158 audit"

# Pattern shorthands for <module> bindings.
_ENGINE_REFS = ["legacy_outcome_service_ref", "lifecycle_funnel_service_ref",
                "doctrine_comparison_service_ref", "platform_sql_attribution_ref"]
_LIFECYCLE_MARKERS = ["lifecycle_sql_column_ref", "lifecycle_sql_property_ref",
                      "lifecycle_sql_stage_ref"]
_LEGACY_MARKERS = ["legacy_sql_literal", "legacy_python_comparison", "legacy_qualified_symbol",
                   "sql_case_expression"]

RULES: list[dict] = [
    # ── the audit itself (diagnostic; its own modules quote every marker) ───
    _m("audit.core.module", "analysis/sql_doctrine_audit.py", CLS_DIAGNOSTIC, _AUDIT,
       _ENGINE_REFS + _LIFECYCLE_MARKERS + _LEGACY_MARKERS
       + ["confirmed_sqls_ref", "contact_created_at_ref", "cpql_ref", "sql_count_ref",
          "sql_verdict_ref", "sqls_field_ref", "sql_reconciliation_ref"]),
    _r("audit.core", "analysis/sql_doctrine_audit.py", CLS_DIAGNOSTIC, _AUDIT,
       symbol=["_difference_reason_codes", "compare_window", "hidden_reconciliation_causes",
               "_counterfactual_counts", "legacy_reconciliation_reasons", "legacy_scope_keys",
               "lifecycle_scope_keys", "render_human", "classification_gap_breakdown",
               "campaign_identity_breakdown", "lifecycle_campaign_breakdown",
               "coverage_gaps", "assemble_report", "write_safety_proof",
               "lifecycle_reasons_not_about_sql", "_scope_set_delta"]),
    _m("audit.registry.module", "analysis/sql_doctrine_registry.py", CLS_DIAGNOSTIC, _AUDIT,
       _ENGINE_REFS + _LIFECYCLE_MARKERS + _LEGACY_MARKERS
       + ["confirmed_sqls_ref", "contact_created_at_ref", "cpql_ref", "sql_count_ref",
          "sql_verdict_ref", "sqls_field_ref", "sql_reconciliation_ref", "frontend_sqls_label"]),
    _r("audit.registry", "analysis/sql_doctrine_registry.py", CLS_DIAGNOSTIC, _AUDIT,
       symbol=["_c", "_r", "_m"]),
    _m("audit.cli.module", "scripts/audit_sql_doctrine_inventory.py", CLS_DIAGNOSTIC, _AUDIT,
       _ENGINE_REFS + ["contact_created_at_ref", "cpql_ref", "lifecycle_sql_column_ref"]),
    _r("audit.cli", "scripts/audit_sql_doctrine_inventory.py", CLS_DIAGNOSTIC, _AUDIT,
       symbol=["_sources_block", "build_runtime_comparison", "fetch_runtime_sources",
               "production_keyword_keys", "production_resolver_factory", "resolve_all_windows",
               "build_static", "write_safety", "run_audit", "main", "install_read_only_guard",
               "ReadOnlyPool"]),

    # ── doctrine engines ────────────────────────────────────────────────────
    _m("engine.legacy.module", "services/canonical_contact_outcome_service.py", CLS_LEGACY,
       _LEGACY_ENGINE, ["contact_created_at_ref", "legacy_outcome_service_ref",
                        "legacy_qualified_symbol", "legacy_sql_literal", "sqls_field_ref"]),
    _r("engine.legacy", "services/canonical_contact_outcome_service.py", CLS_LEGACY, _LEGACY_ENGINE,
       symbol=["_canonical_contact", "_classification_state", "_excluded_contact", "build",
               "build_populations", "page_reconciliation", "reconciliation_metadata",
               "_reconciliation_status", "resolve_window_contract", "_window_block",
               "_scope_counts", "scope_keys", "_scope_block_reasons", "_in_window",
               "deduplicate_latest", "_build_identity_resolver"]),
    _m("engine.legacy.repo.module", "db/canonical_contact_outcome_repository.py", CLS_LEGACY,
       _LEGACY_ENGINE, ["contact_created_at_ref", "legacy_outcome_service_ref"]),
    _r("engine.legacy.repo", "db/canonical_contact_outcome_repository.py", CLS_LEGACY, _LEGACY_ENGINE,
       symbol=["_unavailable", "fetch_canonical_inputs"]),
    _m("engine.funnel.module", "services/canonical_crm_funnel_service.py", CLS_CANONICAL,
       _FUNNEL_ENGINE, ["legacy_outcome_service_ref", "legacy_sql_literal",
                        "lifecycle_funnel_service_ref", "lifecycle_sql_property_ref",
                        "lifecycle_sql_stage_ref"]),
    _r("engine.funnel", "services/canonical_crm_funnel_service.py", CLS_CANONICAL, _FUNNEL_ENGINE,
       symbol=["_build_campaign_resolver", "build", "contacts", "default_campaign_resolver",
               "operational_status_breakdown", "build_populations", "reconciliation_status",
               "_contact_scopes", "_event_counts", "scope_keys", "scopes_are_nested",
               "lead_cohort_progression", "event_definition"]),
    _m("engine.funnel.repo.module", "db/crm_funnel_repository.py", CLS_CANONICAL, _FUNNEL_ENGINE,
       ["lifecycle_funnel_service_ref", "lifecycle_sql_column_ref"]),
    _r("engine.funnel.repo.legacy_rows", "db/crm_funnel_repository.py", CLS_DIAGNOSTIC, _FUNNELREC,
       symbol="fetch_legacy_outcome_rows"),
    _r("engine.funnel.repo", "db/crm_funnel_repository.py", CLS_CANONICAL, _FUNNEL_ENGINE,
       symbol=["fetch_funnel_contacts", "fetch_all_funnel_contacts", "fetch_funnel_contact_page",
               "fetch_operational_status_counts", "fetch_contacts_missing_stage_dates",
               "_funnel_select", "_effective_date_sql", "_recovery_join"]),
    _m("engine.lifecycle.taxonomy", "analysis/crm_lifecycle.py", CLS_CANONICAL, _FUNNEL_ENGINE,
       _LIFECYCLE_MARKERS),
    _m("engine.platform.module", "services/platform_sql_attribution_service.py", CLS_LEGACY,
       _PLATFORM, ["contact_created_at_ref", "platform_sql_attribution_ref"]),
    _r("engine.platform", "services/platform_sql_attribution_service.py", CLS_LEGACY, _PLATFORM,
       symbol=["fetch_and_resolve_contacts", "_attribute", "attribute_keywords",
               "attribute_search_terms", "_keyword_audit", "_search_term_audit",
               "contact_details_for_keys", "_completeness", "_row_state"]),
    _m("engine.platform.repo.module", "db/platform_sql_attribution_repository.py", CLS_LEGACY,
       _PLATFORM, ["contact_created_at_ref", "legacy_sql_literal", "platform_sql_attribution_ref"]),
    _r("engine.platform.repo", "db/platform_sql_attribution_repository.py", CLS_LEGACY, _PLATFORM,
       symbol="fetch_sql_contacts"),

    # ── platform evidence services ──────────────────────────────────────────
    _m("ce.module", "services/campaign_evidence_service.py", CLS_LEGACY, _CE,
       ["contact_created_at_ref", "legacy_qualified_symbol", "sql_verdict_ref"]),
    _r("ce.drawer", "services/campaign_evidence_service.py", CLS_LEGACY, _CD,
       symbol=["build_campaign_drawer_evidence", "_lead_split", "_country_split",
               "build_campaign_evidence_row"]),
    _r("ce.service", "services/campaign_evidence_service.py", CLS_LEGACY, _CE,
       symbol=["_add_lead", "_audit_block", "_build_summary", "_canonical_sql_reconciliation",
               "_new_outcomes", "_outcome_status", "_row", "build_campaign_evidence",
               "unavailable_response", "_junk_rate", "_empty_summary"]),
    _m("kw.module", "services/keyword_evidence_service.py", CLS_LEGACY, _KW, ["sql_verdict_ref"]),
    _r("kw.service", "services/keyword_evidence_service.py", CLS_LEGACY, _KW,
       symbol=["_canonical_keyword_reconciliation", "_filter_sql_state", "_keyword_drawer_sql_block",
               "_keyword_sql_attribution", "_sql_attribution_block", "build_keyword_evidence",
               "_apply_keyword_sql", "build_keyword_drawer", "build_campaign_keyword_preview",
               "_sort_rows"]),
    _m("st.module", "services/search_term_evidence_service.py", CLS_LEGACY, _ST, ["sql_verdict_ref"]),
    _r("st.service", "services/search_term_evidence_service.py", CLS_LEGACY, _ST,
       symbol=["_canonical_st_reconciliation", "_filter_flagged_rows", "_filter_units_sql",
               "_search_term_drawer_sql_block", "_search_term_sql_attribution",
               "_search_term_sql_block", "build_flagged_search_terms", "build_search_term_evidence",
               "_apply_search_term_sql", "_flagged_kpis", "_flagged_priority",
               "_flagged_truth_state", "build_search_term_drawer", "build_campaign_flagged_preview",
               "_empty_flagged_kpis"]),
    _r("workbench", "services/campaign_identity_service.py", CLS_LEGACY, _WORKBENCH,
       symbol="build_mapping_review"),

    # ── executive / revenue services ────────────────────────────────────────
    _m("ovw.module", "services/dashboard_overview_service.py", CLS_MIXED, _OVW, ["sqls_field_ref"]),
    _r("ovw.lifecycle", "services/dashboard_overview_service.py", CLS_CANONICAL, _OVW_LC,
       symbol=["_lifecycle_funnel_block", "_lifecycle_previous_period", "_lifecycle_activity_block"]),
    _r("ovw", "services/dashboard_overview_service.py", CLS_MIXED, _OVW,
       symbol=["_build_decision_cards", "_build_period_change", "_build_signals",
               "_build_source_mix", "_build_unavailable", "build_dashboard_overview",
               "_build_kpis"]),
    _r("mart", "services/revenue_decision_mart.py", CLS_LEGACY, _MART,
       symbol=["_summary_block", "build_revenue_decision_mart", "_canonical_core"]),
    _m("ras.module", "services/revenue_attribution_service.py", CLS_MIXED, _RAS, ["sqls_field_ref"]),
    _r("ras.json", "services/revenue_attribution_service.py", CLS_INACTIVE, _JSONFALLBACK,
       symbol=["_build_from_json", "_build_campaign_rows", "_build_country_rows", "_build_summary"]),
    _r("ras", "services/revenue_attribution_service.py", CLS_MIXED, _RAS,
       symbol=["_build_db_rows", "_build_db_summary", "_build_from_db", "_finalize_row",
               "_geo_spend_only_residual_row", "_new_bucket", "_row_notes",
               "build_revenue_attribution_audit", "classify_verdict", "build_revenue_attribution"]),
    _m("src.module", "services/source_attribution_service.py", CLS_MIXED, _SRC, ["contact_created_at_ref"]),
    _r("src", "services/source_attribution_service.py", CLS_MIXED, _SRC,
       symbol=["_finalize_channels", "_source_contact_row", "_unavailable_revenue_by_source",
               "build_revenue_by_source", "build_source_platform_detail", "classify_contact_row"]),
    _r("chan", "services/dashboard_channels_service.py", CLS_MIXED, _CHAN,
       symbol=["_accumulate", "_build_channels_and_platforms", "_build_decision_cards",
               "_build_kpis", "_build_period_change", "_build_quality_matrix", "_build_trend",
               "_build_truth_status", "_new_bucket", "build_dashboard_channels"]),
    _m("camp.module", "services/dashboard_campaigns_service.py", CLS_LEGACY, _CAMP, ["sql_verdict_ref"]),
    _r("camp", "services/dashboard_campaigns_service.py", CLS_LEGACY, _CAMP,
       symbol=["_build_campaign_rows", "_build_keyword_themes", "_build_kpis",
               "_build_period_change", "_build_unavailable", "_campaign_status",
               "build_dashboard_campaigns"]),
    _m("ctry.module", "services/dashboard_countries_service.py", CLS_LEGACY, _CTRY, ["sql_verdict_ref"]),
    _r("ctry", "services/dashboard_countries_service.py", CLS_LEGACY, _CTRY,
       symbol=["_build_country_rows", "_build_kpis", "_build_period_change", "_build_regional_mix",
               "_build_residual", "_build_unavailable", "_country_status",
               "build_dashboard_countries", "_residual_gap"]),
    _m("rev.module", "services/dashboard_revenue_service.py", CLS_LEGACY, _REV, ["contact_created_at_ref"]),
    _r("rev", "services/dashboard_revenue_service.py", CLS_LEGACY, _REV,
       symbol=["_build_customer_trend", "_build_kpis", "_build_unavailable", "build_dashboard_revenue"]),
    _m("deals.module", "services/dashboard_deals_service.py", CLS_LEGACY, _DEALS, ["sql_verdict_ref"]),
    _r("deals", "services/dashboard_deals_service.py", CLS_LEGACY, _DEALS,
       symbol=["_build_campaign_breakdown", "_build_decision_cards", "_build_funnel", "_build_kpis",
               "_build_period_change", "_build_source_breakdown", "_build_truth_status",
               "_build_unavailable", "_source_status", "build_dashboard_deals",
               "_build_sql_no_deal", "_sql_no_deal_status"]),
    _m("hi.module", "analysis/historical_intelligence.py", CLS_LEGACY, _HI,
       ["confirmed_sqls_ref", "cpql_ref"]),
    _r("hi", "analysis/historical_intelligence.py", CLS_LEGACY, _HI,
       symbol=["_aggregate_campaign_rows", "_aggregate_geo_rows", "_build_deteriorating_note",
               "_build_movement", "_classify_cpql_direction", "_classify_overall_trend",
               "_safe_cpql", "compute_campaign_trends", "compute_geo_trends",
               "compute_quality_movement", "load_campaign_trend_rows"]),

    # ── scheduled outputs ───────────────────────────────────────────────────
    _m("report.core.module", "analysis/core.py", CLS_LEGACY, _REPORT, ["cpql_ref"]),
    _r("report.core", "analysis/core.py", CLS_LEGACY, _REPORT,
       symbol=["determine_verdict", "run_campaign_truth", "run_lead_quality"]),
    _m("report.rule_advisor.module", "analysis/rule_advisor.py", CLS_LEGACY, _REPORT, ["cpql_ref"]),
    _r("report.rule_advisor", "analysis/rule_advisor.py", CLS_LEGACY, _REPORT,
       symbol=["_build_campaign_truth_table", "_build_data_gaps",
               "_build_historical_intelligence_block"]),
    _r("report.advisor", "analysis/advisor.py", CLS_INACTIVE, _CLAUDE,
       symbol=["generate_weekly_report", "generate_monthly_report", "_build_prompt"]),
    # scheduler/ has NO SQL occurrence at the audited commit and NO binding: a
    # new one there is unknown_requires_review by construction.
    _r("windsor", "connectors/windsor_pull.py", CLS_GOOGLE_ADS_CONVERSION, _GACONV,
       symbol="pull_keyword_performance"),
    _m("hubspot_pull.module", "connectors/hubspot_pull.py", CLS_CANONICAL, _SYNC,
       ["lifecycle_sql_property_ref"]),
    _r("hubspot_pull", "connectors/hubspot_pull.py", CLS_CANONICAL, _SYNC,
       symbol="normalize_contact_funnel_row"),
    _r("sync.funnel", "services/hubspot_contact_funnel_sync_service.py", CLS_CANONICAL, _SYNC,
       symbol=["build_coverage", "run_contact_funnel_sync"]),
    _r("recovery", "services/lifecycle_history_recovery_service.py", CLS_CANONICAL, _RECOVERY,
       symbol=["recover", "run_recovery"]),
    _r("recovery.cli", "scripts/backfill_lifecycle_stage_history.py", CLS_CANONICAL, _RECOVERY,
       symbol="main"),
    _m("leadrec.module", "services/lead_reconciliation_service.py", CLS_MIXED, _LEADREC,
       ["contact_created_at_ref"]),
    _m("revrecovery.module", "services/revenue_recovery_service.py", CLS_MIXED, _LEADREC,
       ["contact_created_at_ref"]),
    _m("classrepair.module", "services/canonical_classification_repair_service.py", CLS_LEGACY,
       _WRITE_CLASS, ["contact_created_at_ref", "legacy_outcome_service_ref"]),
    _r("classrepair", "services/canonical_classification_repair_service.py", CLS_LEGACY, _WRITE_CLASS,
       symbol=["_canonical_classification_row", "run_repair"]),
    _m("mailchimp.svc.module", "services/mailchimp_audit_service.py", CLS_LEGACY, _MAILCHIMP,
       ["contact_created_at_ref", "legacy_sql_literal"]),
    _r("mailchimp.svc", "services/mailchimp_audit_service.py", CLS_LEGACY, _MAILCHIMP,
       symbol="build_attribution_audit"),
    _r("mailchimp.repo", "db/mailchimp_repository.py", CLS_LEGACY, _MAILCHIMP,
       symbol="fetch_durable_outcome_populations"),

    # ── writers / schema ────────────────────────────────────────────────────
    _r("writers.campaigns", "db/writers.py", CLS_LEGACY, _WRITE_CAMP, symbol="write_campaigns"),
    _r("writers.leads", "db/writers.py", CLS_LEGACY, _WRITE_LEADS,
       symbol=["write_leads", "_map_status_category", "backfill_event_date_for_contact"]),
    _r("writers.gclid", "db/writers.py", CLS_LEGACY, _GCLID, symbol="write_gclid_attribution"),
    _r("writers.class", "db/writers.py", CLS_LEGACY, _WRITE_CLASS,
       symbol="upsert_contact_source_classification"),
    _r("writers.funnel", "db/writers.py", CLS_CANONICAL, _SYNC, symbol="upsert_hubspot_contact_funnel"),
    _m("writers.module.legacy", "db/writers.py", CLS_LEGACY, _WRITE_LEADS, ["legacy_qualified_symbol"]),
    _m("writers.module.funnel", "db/writers.py", CLS_CANONICAL, _SYNC, ["lifecycle_sql_column_ref"]),
    _m("schema.funnel", "db/schema.py", CLS_CANONICAL, _SYNC, ["lifecycle_sql_column_ref"]),
    _m("schema.legacy", "db/schema.py", CLS_LEGACY, _WRITE_LEADS,
       ["confirmed_sqls_ref", "contact_created_at_ref"]),

    # ── revenue repository (query source of the legacy pages) ───────────────
    _r("revrepo.lead_quality", "db/revenue_repository.py", CLS_LEGACY, _CE,
       symbol=["fetch_lead_quality", "fetch_campaign_lead_detail", "fetch_campaign_pollution_report",
               "fetch_lead_date_grain_health", "fetch_missing_event_date_leads"]),
    _r("revrepo.source", "db/revenue_repository.py", CLS_LEGACY, _SRC,
       symbol=["fetch_source_leads", "fetch_source_leads_daily", "fetch_source_contact_details"]),
    _r("revrepo.series", "db/revenue_repository.py", CLS_LEGACY, _REV, symbol="fetch_lead_daily_series"),
    _r("revrepo.sql_details", "db/revenue_repository.py", CLS_LEGACY, _DEALS, symbol="fetch_sql_lead_details"),

    # ── api/server.py by route function ─────────────────────────────────────
    _r("api.geo", "api/server.py", CLS_LEGACY, _GEO, symbol="api_leads_country_summary"),
    _r("api.leads", "api/server.py", CLS_LEGACY, _LEADS_API,
       symbol=["api_leads", "_lead_quality_sql_reconciliation"]),
    _r("api.summary", "api/server.py", CLS_LEGACY, _SUMMARY, symbol="api_summary"),
    _r("api.trends", "api/server.py", CLS_LEGACY, _TRENDS,
       symbol=["api_dashboard_trends", "_build_trend_alerts", "_compute_severity"]),
    _r("api.aq.campaign", "api/server.py", CLS_LEGACY, _AQC, symbol="_build_campaign_queue_items"),
    _r("api.aq.geo", "api/server.py", CLS_LEGACY, _AQG, symbol="_build_geo_queue_items"),
    _r("api.campaign_detail", "api/server.py", CLS_LEGACY, _CD, symbol="_build_campaign_detail"),
    _r("api.gclid", "api/server.py", CLS_LEGACY, _GCLID, symbol="api_gclid_attribution"),
    _r("api.keyword", "api/server.py", CLS_LEGACY, _KW,
       symbol=["api_keyword_evidence", "api_keyword_evidence_export", "api_keyword_evidence_detail"]),
    _r("api.search_terms", "api/server.py", CLS_LEGACY, _ST,
       symbol=["api_search_term_evidence", "api_search_term_evidence_export",
               "api_search_term_evidence_flagged", "api_search_term_evidence_term"]),
    _r("api.revenue", "api/server.py", CLS_MIXED, _RAS, symbol="get_revenue_attribution"),
    _r("api.source", "api/server.py", CLS_MIXED, _SRC, symbol="get_revenue_by_source"),
    _m("api.module", "api/server.py", CLS_LEGACY, _REPORT, ["sqls_field_ref"]),
    _r("api.crm_funnel", "api/server.py", CLS_CANONICAL, _FUNNEL_ENGINE,
       symbol=["api_crm_funnel", "api_crm_funnel_contacts", "api_crm_funnel_operational_status",
               "api_crm_funnel_sync", "api_crm_funnel_coverage"]),
    _r("api.crm_funnel_audit", "api/server.py", CLS_DIAGNOSTIC, _FUNNELREC, symbol="api_crm_funnel_audit"),
    _r("api.sql_truth", "api/server.py", CLS_DIAGNOSTIC, _SQLTRUTH,
       symbol=["api_audit_sql_truth", "api_audit_sql_truth_repair"]),

    # ── diagnostics ─────────────────────────────────────────────────────────
    _m("diag.sqltruth.module", "services/sql_truth_audit_service.py", CLS_DIAGNOSTIC, _SQLTRUTH,
       ["doctrine_comparison_service_ref", "legacy_outcome_service_ref"]),
    _r("diag.sqltruth", "services/sql_truth_audit_service.py", CLS_DIAGNOSTIC, _SQLTRUTH,
       symbol=["_window_reconciliation", "run", "build_audit", "_keyword_attributable_keys",
               "_differences", "_dashboard_section", "_source_section", "_keyword_section"]),
    _m("diag.funnelrec.module", "services/crm_funnel_reconciliation_service.py", CLS_DIAGNOSTIC,
       _FUNNELREC, ["doctrine_comparison_service_ref", "legacy_outcome_service_ref",
                    "legacy_qualified_symbol", "legacy_sql_literal", "lifecycle_funnel_service_ref",
                    "lifecycle_sql_property_ref", "lifecycle_sql_stage_ref"]),
    _r("diag.funnelrec", "services/crm_funnel_reconciliation_service.py", CLS_DIAGNOSTIC, _FUNNELREC,
       symbol=["compare_sql_counts", "reconcile_contacts", "run", "_scope_coverage"]),
    _m("diag.parity.module", "services/cross_page_parity_service.py", CLS_DIAGNOSTIC, _PARITY,
       ["contact_created_at_ref", "lifecycle_sql_property_ref", "sql_reconciliation_ref",
        "sqls_field_ref"]),
    _r("diag.parity.cli", "scripts/audit_cross_page_canonical_parity.py", CLS_DIAGNOSTIC, _PARITY,
       symbol="main"),
    _m("diag.cert.module", "scripts/audit_campaign_evidence_certification.py", CLS_DIAGNOSTIC, _CERT,
       ["cpql_ref"]),
    _r("diag.cert", "scripts/audit_campaign_evidence_certification.py", CLS_DIAGNOSTIC, _CERT,
       symbol=["_audit_window", "check_frontend_gates", "check_summary_population_reconciliation",
               "check_publication_rule", "check_reconciliation_scope"]),
    _r("diag.stwaste", "scripts/audit_search_term_waste_truth.py", CLS_MIXED, _STWASTE, symbol="main"),
    _r("diag.funnel_truth", "scripts/audit_crm_funnel_truth.py", CLS_DIAGNOSTIC, _FUNNELREC,
       symbol=["collect", "main"]),
    _r("diag.leads_truth", "scripts/audit_leads_page_truth.py", CLS_DIAGNOSTIC, _FUNNELREC,
       symbol=["collect", "main"]),

    # ── frontend: static/app.js by top-level function ───────────────────────
    _r("ui.campaign", "static/app.js", CLS_LEGACY, _CE,
       symbol=["campaignSqlPublication", "campaignSqlWithheld", "loadCampaignEvidence",
               "renderCampaignEvidenceKPIs", "renderCampaignSqlReconciliation",
               "renderCampaignEvidenceFilters", "filterCampaignEvidence", "sortCampaignEvidence",
               "renderCampaignEvidenceRow", "wireCampaignEvidenceControls", "applyEvidenceChrome",
               "renderCampaignMappingCard", "renderCampaignDecisionTable"]),
    _r("ui.campaign.drawer", "static/app.js", CLS_LEGACY, _CD,
       symbol=["renderCampaignDrawer", "_appendDrawerEvidenceSections", "openCampaignDrawer"]),
    _r("ui.geo", "static/app.js", CLS_LEGACY, _GEO, symbol=["loadGeo", "renderGeoMap", "renderGeoTable"]),
    _r("ui.overview", "static/app.js", CLS_MIXED, _OVW, symbol=["renderDashKpiRow", "dashSignalBody"]),
    _r("ui.channels", "static/app.js", CLS_MIXED, _CHAN,
       symbol=["renderDashboardChannels", "renderChanKpiRow", "chanQualityBubbleSVG",
               "renderChanQualityPanel", "wireChanQualityHover", "renderChanPlatformMatrix",
               "renderChanChannelList", "wireChanChannelDrawers", "renderChanTruthFooter"]),
    _r("ui.campaigns_tab", "static/app.js", CLS_LEGACY, _CAMP,
       symbol=["renderDashboardCampaigns", "renderCampKpiRow", "campBubbleSVG",
               "renderCampPerformanceMap", "wireCampBubbleHover", "renderCampLeaderboard",
               "wireCampDrawers", "renderCampKeywordThemes", "renderCampTruthFooter", "loadCampaigns"]),
    _r("ui.countries_tab", "static/app.js", CLS_LEGACY, _CTRY,
       symbol=["renderDashboardCountries", "renderCtryKpiRow", "wireCtryBubbleHover",
               "renderCtryLeaderboard", "wireCtryDrawers", "renderCtryResidualPanel",
               "renderCtryTruthFooter", "ctryStatusColor", "renderCountryRevenueBlockedState"]),
    _r("ui.revenue_tab", "static/app.js", CLS_LEGACY, _REV,
       symbol=["renderRevKpiRow", "renderRevConversionStrip", "renderRevTruthFooter",
               "renderRevenueBlockedState", "renderRevenueHealth"]),
    _r("ui.deals_tab", "static/app.js", CLS_LEGACY, _DEALS,
       symbol=["renderDashboardDeals", "renderDealKpiRow", "renderDealSourcePipeline",
               "renderDealCampaignPipeline", "renderDealSqlNoDeal", "renderDealTruthFooter"]),
    _r("ui.source", "static/app.js", CLS_MIXED, _SRC,
       symbol=["sourceGaSqlsChip", "renderSourceGroupSection", "renderSourceChannelRows",
               "renderSourcePlatformRow", "renderSourceDrilldown"]),
    _r("ui.mart", "static/app.js", CLS_LEGACY, _MART,
       symbol=["renderMartSummaryStrip", "getBusinessVerdictBadge"]),
    _r("ui.roas", "static/app.js", CLS_MIXED, _RAS,
       symbol=["renderRoasCampaignSummary", "renderRoasCampaignTable", "sortRoasCampaignRows",
               "renderRoasCountrySummary", "renderRoasCountryTable", "sortRoasCountryRows",
               "summarizeRoasCountryRows"]),
    _r("ui.keywords", "static/app.js", CLS_LEGACY, _KW,
       symbol=["kwSqlCoverageNote", "renderKeywordFilters", "renderKeywordTable", "kwDrawerSqlSection"]),
    _r("ui.search_terms", "static/app.js", CLS_LEGACY, _ST,
       symbol=["renderTermsFilters", "renderTermsTable", "wireTermsControls", "stDrawerSqlSection",
               "renderFlaggedFilters", "renderFlaggedKPIs", "renderFlaggedTab", "renderFlaggedTable",
               "openFlaggedDrawer"]),
    _r("ui.historical", "static/app.js", CLS_LEGACY, _HI, symbol="_renderHistoricalTable"),
    _r("ui.gclid", "static/app.js", CLS_LEGACY, _GCLID, symbol="renderGclidAttributionTable"),
    _r("ui.report", "static/app.js", CLS_LEGACY, _REPORT, symbol="copyLatestReport"),
    _m("ui.index", "static/index.html", CLS_LEGACY, _GEO,
       ["confirmed_sqls_ref", "frontend_sqls_label", "sqls_field_ref"]),
]


# ═════════════════════════════════════════════════════════════════════════════
# CPQL CONSUMERS (§8)
# ═════════════════════════════════════════════════════════════════════════════
CPQL_CONSUMERS: list[dict] = [
    {
        "consumer": "Campaign Evidence row cpql_usd",
        "code_location": "services/campaign_evidence_service.py:_row",
        "spend_numerator_source": "google_ads_campaign_daily_spend (canonical) per campaign_id, selected window",
        "currency_fx_contract": "native GBP → USD via fx_rates per spend_date; usd None when any date lacks FX",
        "sql_denominator_definition": LEGACY_DEF,
        "sql_denominator_scope": "campaign_attributable (mapped rows)",
        "sql_denominator_date_field": LEGACY_DATE,
        "numerator_denominator_same_window": True,
        "unmatched_sqls_excluded": True,
        "denominator_incomplete_when_stage_dates_missing": "n/a (legacy denominator does not use stage dates); lifecycle 30d shows 40 SQL-stage contacts without entry date",
        "publication": "published per row; withheld in UI unless reconciliation_status == reconciled",
    },
    {
        "consumer": "Campaign Evidence overall_cpql_usd",
        "code_location": "services/campaign_evidence_service.py:_build_summary",
        "spend_numerator_source": "account-wide canonical USD spend total (all Google Ads campaigns)",
        "currency_fx_contract": "total_spend_usd None unless fx_complete",
        "sql_denominator_definition": LEGACY_DEF,
        "sql_denominator_scope": "campaign_attributable (mapped_sqls only)",
        "sql_denominator_date_field": LEGACY_DATE,
        "numerator_denominator_same_window": True,
        "unmatched_sqls_excluded": True,
        "denominator_incomplete_when_stage_dates_missing": "n/a (legacy); overall_cpql_scope = complete|mapped_only discloses unmatched/excluded SQLs",
        "publication": "labelled confirmed-subset (overall_cpql_scope); withheld in UI unless reconciled",
    },
    {
        "consumer": "/api/summary avg_cpql_usd",
        "code_location": "api/server.py:api_summary",
        "spend_numerator_source": "SUM(campaigns.spend_usd) snapshot at latest run",
        "currency_fx_contract": "snapshot column labelled USD; no FX contract",
        "sql_denominator_definition": SNAPSHOT_DEF,
        "sql_denominator_scope": "other (per campaign label, undeduplicated)",
        "sql_denominator_date_field": SNAPSHOT_DATE,
        "numerator_denominator_same_window": "both from the same snapshot run; neither is a business window",
        "unmatched_sqls_excluded": False,
        "denominator_incomplete_when_stage_dates_missing": "n/a",
        "publication": "published without reconciliation",
    },
    {
        "consumer": "Historical Intelligence cpql movement",
        "code_location": "analysis/historical_intelligence.py:_safe_cpql",
        "spend_numerator_source": "campaigns.spend_usd snapshot rows, 30d vs prior 30d by run_date",
        "currency_fx_contract": "snapshot column labelled USD; no FX contract",
        "sql_denominator_definition": "COALESCE(confirmed_sqls, 0) from campaigns snapshot",
        "sql_denominator_scope": "other (per campaign label)",
        "sql_denominator_date_field": SNAPSHOT_DATE,
        "numerator_denominator_same_window": True,
        "unmatched_sqls_excluded": False,
        "denominator_incomplete_when_stage_dates_missing": "n/a; missing value coerced to 0",
        "publication": "published; drives improving/deteriorating verdicts",
    },
    {
        "consumer": "campaigns.cpql_usd snapshot column (weekly/monthly report table)",
        "code_location": "analysis/core.py:run_campaign_truth",
        "spend_numerator_source": "Google Ads 30d spend from the report pull",
        "currency_fx_contract": "none (labelled USD)",
        "sql_denominator_definition": "mql_status in QUALIFIED from data/crm_contacts.json",
        "sql_denominator_scope": "other (per campaign label, undated, undeduplicated)",
        "sql_denominator_date_field": "none",
        "numerator_denominator_same_window": False,
        "unmatched_sqls_excluded": False,
        "denominator_incomplete_when_stage_dates_missing": "n/a",
        "publication": "published in the emailed report table; N/A on 0 SQLs",
    },
    {
        "consumer": "Dashboard Revenue revenue_per_sql_usd (inverse-CPQL family)",
        "code_location": "services/dashboard_revenue_service.py:_build_kpis",
        "spend_numerator_source": "n/a (revenue ÷ SQLs)",
        "currency_fx_contract": "canonical revenue USD",
        "sql_denominator_definition": LEGACY_DEF,
        "sql_denominator_scope": "campaign_attributable",
        "sql_denominator_date_field": LEGACY_DATE,
        "numerator_denominator_same_window": True,
        "unmatched_sqls_excluded": True,
        "denominator_incomplete_when_stage_dates_missing": "n/a (legacy)",
        "publication": "published; None on None/0 denominator",
    },
]


# ═════════════════════════════════════════════════════════════════════════════
# DECISION SURFACES (§8)
# ═════════════════════════════════════════════════════════════════════════════
DECISION_SURFACES: list[dict] = [
    {"surface": "Campaign Evidence outcome_status 'SQL producer' / 'Spend without SQL proof'",
     "code_location": "services/campaign_evidence_service.py:_outcome_status",
     "sql_definition": LEGACY_DEF, "scope": "campaign_attributable", "date_field": LEGACY_DATE,
     "kind": "verdict", "gated": "frontend campaignSqlPublication() only",
     "evidence_kind": "row evidence"},
    {"surface": "Campaign Evidence has_sql / no_sql outcome filter",
     "code_location": "static/app.js:filterCampaignEvidence",
     "sql_definition": LEGACY_DEF, "scope": "campaign_attributable", "date_field": LEGACY_DATE,
     "kind": "filter", "gated": "disabled unless reconciled", "evidence_kind": "row evidence"},
    {"surface": "Campaign Evidence sqls / cpql sort",
     "code_location": "static/app.js:sortCampaignEvidence",
     "sql_definition": LEGACY_DEF, "scope": "campaign_attributable", "date_field": LEGACY_DATE,
     "kind": "sort", "gated": "disabled unless reconciled", "evidence_kind": "row evidence"},
    {"surface": "Keyword Evidence sql_state filter and attributed_sqls sort",
     "code_location": "services/keyword_evidence_service.py:_filter_sql_state",
     "sql_definition": LEGACY_DEF, "scope": "keyword_attributable", "date_field": LEGACY_DATE,
     "kind": "filter+sort", "gated": "never disabled", "evidence_kind": "row evidence"},
    {"surface": "Search Terms sql_state filter, attributed_sqls sort, flagged priority (+15 proven zero)",
     "code_location": "services/search_term_evidence_service.py:_flagged_priority",
     "sql_definition": LEGACY_DEF, "scope": "campaign_attributable / unit", "date_field": LEGACY_DATE,
     "kind": "filter+sort+priority", "gated": "never disabled", "evidence_kind": "row evidence"},
    {"surface": "Campaign verdict FIX/HOLD/SCALE/CUT (weekly/monthly report)",
     "code_location": "analysis/core.py:determine_verdict",
     "sql_definition": "mql_status in QUALIFIED (JSON)", "scope": "per campaign label",
     "date_field": "none", "kind": "verdict", "gated": "none",
     "evidence_kind": "complete total claimed (undated, undeduplicated)"},
    {"surface": "Weekly recommendations: Data Gaps '0 confirmed SQLs with spend'",
     "code_location": "analysis/rule_advisor.py:_build_data_gaps",
     "sql_definition": "campaign_truth confirmed_sqls (JSON)", "scope": "per campaign label",
     "date_field": "none", "kind": "recommendation", "gated": "none", "evidence_kind": "row evidence"},
    {"surface": "Action Queue campaign items (sqls == 0 and spend > 0 → +30)",
     "code_location": "api/server.py:_build_campaign_queue_items",
     "sql_definition": SNAPSHOT_DEF, "scope": "per campaign label", "date_field": SNAPSHOT_DATE,
     "kind": "action-queue", "gated": "none", "evidence_kind": "row evidence"},
    {"surface": "Action Queue geo items (spend > 0 and sqls == 0 → +20)",
     "code_location": "api/server.py:_build_geo_queue_items",
     "sql_definition": "SUM(CASE WHEN status_category = 'qualified' …)", "scope": "country (all_source)",
     "date_field": "run_date", "kind": "action-queue", "gated": "none", "evidence_kind": "row evidence"},
    {"surface": "Dashboard trends alert 'spend rose without SQLs' + severity",
     "code_location": "api/server.py:_build_trend_alerts",
     "sql_definition": SNAPSHOT_DEF, "scope": "per campaign label", "date_field": SNAPSHOT_DATE,
     "kind": "alert", "gated": "insufficient_data guard", "evidence_kind": "row evidence"},
    {"surface": "Dashboard Overview decision cards / waste signals",
     "code_location": "services/dashboard_overview_service.py:_build_decision_cards",
     "sql_definition": LEGACY_DEF, "scope": "campaign_attributable", "date_field": LEGACY_DATE,
     "kind": "decision-card", "gated": "explicit 'SQL truth unavailable' card when withheld",
     "evidence_kind": "row evidence"},
    {"surface": "Dashboard Campaigns _campaign_status (SQL producer)",
     "code_location": "services/dashboard_campaigns_service.py:_campaign_status",
     "sql_definition": LEGACY_DEF, "scope": "campaign_attributable", "date_field": LEGACY_DATE,
     "kind": "verdict", "gated": "none", "evidence_kind": "row evidence"},
    {"surface": "Dashboard Countries _country_status (SQL producer)",
     "code_location": "services/dashboard_countries_service.py:_country_status",
     "sql_definition": LEGACY_DEF, "scope": "country_attributed", "date_field": LEGACY_DATE,
     "kind": "verdict", "gated": "none", "evidence_kind": "row evidence"},
    {"surface": "Dashboard Deals _source_status",
     "code_location": "services/dashboard_deals_service.py:_source_status",
     "sql_definition": LEGACY_DEF, "scope": "source group", "date_field": LEGACY_DATE,
     "kind": "verdict", "gated": "none", "evidence_kind": "row evidence"},
    {"surface": "Revenue attribution classify_verdict (watch/waste)",
     "code_location": "services/revenue_attribution_service.py:classify_verdict",
     "sql_definition": LEGACY_DEF, "scope": "campaign_attributable / country", "date_field": LEGACY_DATE,
     "kind": "verdict", "gated": "withheld count substituted by 0 (sqls_for_verdict)",
     "evidence_kind": "row evidence"},
    {"surface": "Historical Intelligence improving/deteriorating (CPQL direction)",
     "code_location": "analysis/historical_intelligence.py:_classify_overall_trend",
     "sql_definition": "COALESCE(confirmed_sqls, 0) snapshot", "scope": "per campaign label",
     "date_field": SNAPSHOT_DATE, "kind": "verdict", "gated": "none", "evidence_kind": "row evidence"},
]


# ═════════════════════════════════════════════════════════════════════════════
# KNOWN CONTRACT CONFLICTS (§10/§11 — documented, not fixed here)
# ═════════════════════════════════════════════════════════════════════════════
KNOWN_CONTRACT_CONFLICTS: list[dict] = [
    {"id": "naming.canonical_contact_outcome_is_legacy",
     "summary": "services/canonical_contact_outcome_service.py is named canonical but "
                "declares SQL_DEFINITION = 'latest status_category = qualified' on "
                "contact_created_at; every sql_reconciliation block is a legacy-doctrine block.",
     "code_location": "services/canonical_contact_outcome_service.py:SQL_DEFINITION"},
    {"id": "reconciliation.non_sql_gap_downgrades_sql_status",
     "summary": "_reconciliation_status turns partial on stale/missing classification of ANY "
                "in-window contact; a non-SQL contact (latest status unknown) downgrades the "
                "Campaign Evidence SQL reconciliation to partial and withholds the aggregate.",
     "code_location": "services/canonical_contact_outcome_service.py:_reconciliation_status"},
    {"id": "date.creation_vs_stage_entry",
     "summary": "Legacy consumers window SQLs by contact_created_at; the lifecycle contract "
                "windows by date_entered_sql. The same contact lands in different windows.",
     "code_location": "services/crm_funnel_reconciliation_service.py:compare_sql_counts"},
    {"id": "date.run_date_windows",
     "summary": "Geo country summary, action-queue geo items and /api/leads window the leads "
                "table by run_date; Campaign Evidence windows the same table by "
                "contact_created_at under the same Evidence Window label.",
     "code_location": "api/server.py:api_leads_country_summary"},
    {"id": "doctrine.third_snapshot_population",
     "summary": "campaigns.confirmed_sqls (analysis/core.py) counts mql_status literals from a "
                "JSON pull with no date, dedup or exclusions; /api/summary, "
                "/api/dashboard/trends, action queue and historical intelligence read it.",
     "code_location": "analysis/core.py:run_lead_quality"},
    {"id": "scope.keyword_attributable_two_definitions",
     "summary": "platform_sql_attribution keyword scope is criterion-level (campaign_id + "
                "exact keyword, unique only); the lifecycle funnel keyword scope is "
                "campaign-attributable AND a HubSpot keyword label is present.",
     "code_location": "services/canonical_crm_funnel_service.py:_contact_scopes"},
    {"id": "lifecycle.status_partial_on_non_sql_events",
     "summary": "canonical_crm_funnel_service.reconciliation_status reports partial when ANY "
                "event lacks stage dates or an unknown stage exists, so the SQL event can be "
                "complete while the funnel status is partial.",
     "code_location": "services/canonical_crm_funnel_service.py:reconciliation_status"},
    {"id": "lifecycle.contact_page_without_recovery_coalesce",
     "summary": "fetch_funnel_contact_page and fetch_operational_status_counts filter the bare "
                "date_entered_sql column; the headline read COALESCEs recovered history dates. "
                "Headline SQL count can exceed the contact-page total.",
     "code_location": "db/crm_funnel_repository.py:fetch_funnel_contact_page"},
    {"id": "ui.channels_sums_none_as_zero",
     "summary": "Dashboard Channels total_sqls sums withheld channels as 0.",
     "code_location": "services/dashboard_channels_service.py:_build_kpis"},
    {"id": "ui.geo_none_to_zero",
     "summary": "Geo page renders a country absent from the lead summary as 0 SQLs and labels "
                "Google Ads conversions 'Conv.' beside 'SQLs' with no qualifier.",
     "code_location": "static/app.js:loadGeo"},
    {"id": "ui.campaign_drawer_tdz",
     "summary": "renderCampaignDrawer reads drawerSqlPub before its const declaration "
                "(temporal dead zone) — the drawer's SQL gate cannot execute as written.",
     "code_location": "static/app.js:renderCampaignDrawer"},
    {"id": "verdict.withheld_sql_becomes_zero",
     "summary": "revenue_attribution_service.classify_verdict uses sqls_for_verdict = 0 when "
                "lead metrics are withheld.",
     "code_location": "services/revenue_attribution_service.py:classify_verdict"},
    {"id": "label.search_term_waste_audit_says_lifecycle",
     "summary": "scripts/audit_search_term_waste_truth.py prints 'lifecycle SQLs' for a "
                "status_category = qualified population.",
     "code_location": "scripts/audit_search_term_waste_truth.py:main"},
    {"id": "gate.backend_never_enforces_reconciliation",
     "summary": "No backend withholds an SQL aggregate on a non-reconciled status; every "
                "publication rule lives in static/app.js and applies only to Campaign Evidence.",
     "code_location": "static/app.js:campaignSqlPublication"},
]

__all__ = ["CONSUMERS", "RULES", "CPQL_CONSUMERS", "DECISION_SURFACES",
           "KNOWN_CONTRACT_CONFLICTS"]
