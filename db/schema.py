"""
db/schema.py

PostgreSQL schema for the Logistaas Ads Intelligence System.

Responsibility:
  - Define all tables: runs, campaigns, leads, waste_terms, deals.
  - Provide init_db() which creates all tables and indexes if they do not
    already exist (idempotent — safe to call on every startup).
  - Non-fatal: if the database is unavailable, init_db() logs and returns
    without raising.

Call once at application startup:
    from db.connection import init_pool
    from db.schema import init_db
    init_pool()
    init_db()
"""

import logging

from db.connection import get_conn

log = logging.getLogger(__name__)

_DDL = """
-- One row per scheduler run
CREATE TABLE IF NOT EXISTS runs (
    id                  SERIAL PRIMARY KEY,
    run_type            VARCHAR(20)  NOT NULL,
    started_at          TIMESTAMPTZ  NOT NULL,
    finished_at         TIMESTAMPTZ,
    status              VARCHAR(20)  NOT NULL,
    failed_step         TEXT,
    error_message       TEXT,
    report_path         TEXT,
    delivery_attempted  BOOLEAN      DEFAULT FALSE,
    delivery_success    BOOLEAN
);

-- One row per campaign per run
CREATE TABLE IF NOT EXISTS campaigns (
    id                  SERIAL PRIMARY KEY,
    run_id              INTEGER      NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    run_date            DATE         NOT NULL,
    campaign_name       TEXT         NOT NULL,
    spend_usd           NUMERIC(10,2),
    clicks              INTEGER,
    impressions         INTEGER,
    conversions         NUMERIC(8,2),
    total_leads         INTEGER,
    confirmed_sqls      INTEGER,
    junk_count          INTEGER,
    junk_rate_pct       NUMERIC(5,2),
    cpql_usd            NUMERIC(10,2),
    verdict             VARCHAR(10),
    verdict_reason      TEXT,
    created_at          TIMESTAMPTZ  DEFAULT NOW()
);

-- One row per HubSpot contact per run
CREATE TABLE IF NOT EXISTS leads (
    id                  SERIAL PRIMARY KEY,
    run_id              INTEGER      NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    run_date            DATE         NOT NULL,
    contact_id          TEXT,
    campaign_name       TEXT,
    keyword             TEXT,
    country             TEXT,
    mql_status          TEXT,
    status_category     VARCHAR(20),
    gclid               TEXT,
    source_type         VARCHAR(30),
    company             TEXT,
    created_at          TIMESTAMPTZ  DEFAULT NOW()
);

-- One row per waste term per run
CREATE TABLE IF NOT EXISTS waste_terms (
    id                  SERIAL PRIMARY KEY,
    run_id              INTEGER      NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    run_date            DATE         NOT NULL,
    search_term         TEXT         NOT NULL,
    campaign_name       TEXT,
    spend_usd           NUMERIC(10,2),
    junk_category       TEXT,
    matched_pattern     TEXT,
    crm_junk_confirmed  INTEGER      DEFAULT 0,
    created_at          TIMESTAMPTZ  DEFAULT NOW()
);

-- One row per GCLID-matched deal per run
CREATE TABLE IF NOT EXISTS deals (
    id                  SERIAL PRIMARY KEY,
    run_id              INTEGER      NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    run_date            DATE         NOT NULL,
    contact_id          TEXT,
    company             TEXT,
    country             TEXT,
    keyword             TEXT,
    campaign_name       TEXT,
    deal_stage          TEXT,
    deal_stage_label    TEXT,
    deal_amount_usd     NUMERIC(12,2),
    mql_status          TEXT,
    gclid               TEXT,
    created_at          TIMESTAMPTZ  DEFAULT NOW()
);

-- Indexes for time-range queries
CREATE INDEX IF NOT EXISTS idx_campaigns_run_date ON campaigns(run_date);
CREATE INDEX IF NOT EXISTS idx_leads_run_date     ON leads(run_date);
CREATE INDEX IF NOT EXISTS idx_waste_run_date     ON waste_terms(run_date);
CREATE INDEX IF NOT EXISTS idx_deals_run_date     ON deals(run_date);
CREATE INDEX IF NOT EXISTS idx_campaigns_name     ON campaigns(campaign_name);

-- PR-ADS-026: company name on leads (idempotent migration for existing DBs)
-- New installs: company is already in the CREATE TABLE above; ALTER is a no-op.
-- Existing DBs: ALTER TABLE adds the column; historical rows will have company NULL
--   until the next scheduler run that calls write_leads() populates them — this is
--   expected and handled by frontend.
ALTER TABLE leads ADD COLUMN IF NOT EXISTS company TEXT;

-- PR-ADS-025C: source type tracking + index (idempotent migration for existing DBs)
-- New installs: source_type is already in the CREATE TABLE above; ALTER is a no-op.
-- Existing DBs: ALTER TABLE adds the column; existing rows will have source_type NULL
--   until the next weekly run populates them — this is expected and handled by frontend.
ALTER TABLE leads ADD COLUMN IF NOT EXISTS source_type VARCHAR(30);
CREATE INDEX IF NOT EXISTS idx_leads_source_type    ON leads(source_type);
-- PR-ADS-025E-FIX: index on leads(campaign_name) to prevent full table scans on backfill UPDATEs
CREATE INDEX IF NOT EXISTS idx_leads_campaign_name  ON leads(campaign_name);

-- PR-ADS-109: business event date + raw HubSpot source on leads (idempotent migration)
-- run_date stays the scheduler/sync date. contact_created_at is the HubSpot
-- contact creation date (business event date) used for business-window filtering.
-- Existing rows written before this migration will have contact_created_at NULL
-- until the next sync repopulates them; the revenue-attribution audit reports this
-- as an unsafe date grain (lead metrics withheld) rather than miscounting windows.
ALTER TABLE leads ADD COLUMN IF NOT EXISTS contact_created_at TIMESTAMPTZ;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS hs_analytics_source TEXT;
CREATE INDEX IF NOT EXISTS idx_leads_contact_created_at      ON leads(contact_created_at);
CREATE INDEX IF NOT EXISTS idx_leads_source_type_created     ON leads(source_type, contact_created_at);
CREATE INDEX IF NOT EXISTS idx_leads_paid_campaign_created   ON leads(source_type, campaign_name, contact_created_at);

-- PR-ADS-025E: canonicalise Windsor variant campaign names (idempotent)
-- Authoritative source: _CAMPAIGN_CANONICAL dict in db/writers.py.
-- If you add a new Windsor→canonical mapping there, add the matching UPDATE pair here too.
-- "mexico, chile, colombia" → "mexico,chile": HubSpot UTM tracks this campaign without Colombia in the name.
UPDATE campaigns SET campaign_name = 'mexico,chile'          WHERE campaign_name = 'mexico, chile, colombia';
UPDATE campaigns SET campaign_name = 'compliance - markets'  WHERE campaign_name = 'compliance markets';
UPDATE campaigns SET campaign_name = 'emerging - markets'    WHERE campaign_name = 'emerging markets';
UPDATE campaigns SET campaign_name = 'mature - markets'      WHERE campaign_name = 'mature markets';
UPDATE campaigns SET campaign_name = 'europe low cpc-new'    WHERE campaign_name = 'europe low-cpc-2026';

UPDATE leads SET campaign_name = 'mexico,chile'          WHERE campaign_name = 'mexico, chile, colombia';
UPDATE leads SET campaign_name = 'compliance - markets'  WHERE campaign_name = 'compliance markets';
UPDATE leads SET campaign_name = 'emerging - markets'    WHERE campaign_name = 'emerging markets';
UPDATE leads SET campaign_name = 'mature - markets'      WHERE campaign_name = 'mature markets';
UPDATE leads SET campaign_name = 'europe low cpc-new'    WHERE campaign_name = 'europe low-cpc-2026';

-- PR-ADS-025F: migrations table for one-time idempotent operations
CREATE TABLE IF NOT EXISTS migrations (
    migration_id VARCHAR(50) PRIMARY KEY,
    applied_at   TIMESTAMP DEFAULT NOW()
);

-- PR-ADS-025F: delete junk HubSpot source entries from campaigns table (idempotent)
DELETE FROM campaigns WHERE campaign_name IN (
    '(referral)', '(organic)', '(direct)', '(not set)',
    '(cross-network)', '(none)', '(content)', '(social)'
);
DELETE FROM campaigns WHERE campaign_name ~ '(?i)email_campaign';

-- PR-ADS-029: geo performance per run
CREATE TABLE IF NOT EXISTS geo (
    id              SERIAL PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    run_date        DATE         NOT NULL,
    country         TEXT,
    campaign_name   TEXT,
    spend_usd       NUMERIC(10,2) DEFAULT 0,
    clicks          INTEGER       DEFAULT 0,
    impressions     INTEGER       DEFAULT 0,
    conversions     NUMERIC(8,2)  DEFAULT 0,
    created_at      TIMESTAMPTZ   DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_geo_run_date   ON geo(run_date);
CREATE INDEX IF NOT EXISTS idx_geo_country    ON geo(country);
CREATE INDEX IF NOT EXISTS idx_geo_campaign   ON geo(campaign_name);
CREATE INDEX IF NOT EXISTS idx_geo_run_id     ON geo(run_id);

-- PR-ADS-031: keyword performance per run
CREATE TABLE IF NOT EXISTS keywords (
    id              SERIAL PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    run_date        DATE         NOT NULL,
    campaign_name   TEXT,
    ad_group        TEXT,
    keyword         TEXT,
    match_type      TEXT,
    quality_score   NUMERIC(5,2),
    spend_usd       NUMERIC(10,2) DEFAULT 0,
    clicks          INTEGER       DEFAULT 0,
    impressions     INTEGER       DEFAULT 0,
    conversions     NUMERIC(8,2)  DEFAULT 0,
    cpc_usd         NUMERIC(10,2) DEFAULT 0,
    created_at      TIMESTAMPTZ   DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_keywords_run_date   ON keywords(run_date);
CREATE INDEX IF NOT EXISTS idx_keywords_campaign   ON keywords(campaign_name);
CREATE INDEX IF NOT EXISTS idx_keywords_keyword    ON keywords(keyword);
CREATE INDEX IF NOT EXISTS idx_keywords_match_type ON keywords(match_type);
CREATE INDEX IF NOT EXISTS idx_keywords_run_id     ON keywords(run_id);

-- PR-ADS-039: sync batch audit trail — one row per dataset sync operation
CREATE TABLE IF NOT EXISTS sync_batches (
    id            SERIAL PRIMARY KEY,
    run_id        INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    source        TEXT NOT NULL,
    dataset       TEXT NOT NULL,
    sync_type     TEXT NOT NULL,
    date_from     DATE,
    date_to       DATE,
    started_at    TIMESTAMPTZ DEFAULT NOW(),
    finished_at   TIMESTAMPTZ,
    status        TEXT NOT NULL DEFAULT 'running',
    row_count     INTEGER DEFAULT 0,
    error_message TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sync_batches_source_dataset ON sync_batches(source, dataset);
CREATE INDEX IF NOT EXISTS idx_sync_batches_status         ON sync_batches(status);
CREATE INDEX IF NOT EXISTS idx_sync_batches_started_at     ON sync_batches(started_at);
CREATE INDEX IF NOT EXISTS idx_sync_batches_run_id         ON sync_batches(run_id);

-- PR-ADS-039: sync state / watermark — one row per source+dataset (upserted on each sync)
CREATE TABLE IF NOT EXISTS sync_state (
    id                      SERIAL PRIMARY KEY,
    source                  TEXT NOT NULL,
    dataset                 TEXT NOT NULL,
    last_successful_sync_at TIMESTAMPTZ,
    last_source_date        DATE,
    last_batch_id           INTEGER REFERENCES sync_batches(id) ON DELETE SET NULL,
    status                  TEXT NOT NULL DEFAULT 'unknown',
    error_message           TEXT,
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(source, dataset)
);

CREATE INDEX IF NOT EXISTS idx_sync_state_status         ON sync_state(status);
CREATE INDEX IF NOT EXISTS idx_sync_state_updated_at     ON sync_state(updated_at);

-- PR-ADS-040: full search-term fact table
-- grain: source_date + campaign + ad_group + keyword + match_type + search_term
-- is_flagged_waste tri-state: NULL = not analysed | TRUE = flagged waste | FALSE = analysed clean
-- Do NOT default is_flagged_waste to FALSE — raw writer must leave it NULL.
CREATE TABLE IF NOT EXISTS search_terms (
  id               SERIAL PRIMARY KEY,
  run_id           INTEGER REFERENCES runs(id) ON DELETE SET NULL,
  source_date      DATE         NOT NULL,
  campaign_name    TEXT,
  campaign_id      TEXT,
  ad_group         TEXT,
  keyword          TEXT,
  match_type       TEXT,
  search_term      TEXT          NOT NULL,
  spend_usd        NUMERIC(10,2) DEFAULT 0,
  clicks           INTEGER       DEFAULT 0,
  impressions      INTEGER       DEFAULT 0,
  conversions      NUMERIC(8,2)  DEFAULT 0,

  -- Tri-state analysis flag:
  -- NULL  = not analysed yet
  -- TRUE  = analysed and flagged as waste
  -- FALSE = analysed and not flagged
  is_flagged_waste BOOLEAN,

  junk_category    TEXT,
  matched_pattern  TEXT,

  sync_batch_id    INTEGER REFERENCES sync_batches(id) ON DELETE SET NULL,
  created_at       TIMESTAMPTZ DEFAULT NOW(),
  updated_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Unique natural key: prevents duplicate fact rows for the same search term event
-- COALESCE handles nullable columns; search_term is NOT NULL so listed directly.
CREATE UNIQUE INDEX IF NOT EXISTS idx_search_terms_unique_fact
  ON search_terms (
    source_date,
    COALESCE(campaign_name,  ''),
    COALESCE(ad_group,       ''),
    COALESCE(keyword,        ''),
    COALESCE(match_type,     ''),
    search_term
  );

-- Cursor/keyset pagination index (source_date DESC, id DESC)
CREATE INDEX IF NOT EXISTS idx_search_terms_cursor
  ON search_terms(source_date DESC, id DESC);

-- Lookup indexes
CREATE INDEX IF NOT EXISTS idx_search_terms_source_date
  ON search_terms(source_date);

CREATE INDEX IF NOT EXISTS idx_search_terms_campaign
  ON search_terms(campaign_name);

CREATE INDEX IF NOT EXISTS idx_search_terms_keyword
  ON search_terms(keyword);

CREATE INDEX IF NOT EXISTS idx_search_terms_match_type
  ON search_terms(match_type);

CREATE INDEX IF NOT EXISTS idx_search_terms_flagged_waste
  ON search_terms(is_flagged_waste);

CREATE INDEX IF NOT EXISTS idx_search_terms_sync_batch
  ON search_terms(sync_batch_id);

-- NOTE: Trigram index for /api/search-terms?q= (full-text contains search) requires
-- the pg_trgm extension.  If you have DBA access, enable it once with:
--   CREATE EXTENSION IF NOT EXISTS pg_trgm;
--   CREATE INDEX IF NOT EXISTS idx_search_terms_search_term_trgm
--     ON search_terms USING gin (search_term gin_trgm_ops);
-- Until enabled, ?q= filtering is supported but uses a sequential scan.
-- Do NOT add a plain B-tree index — it does not support LIKE '%term%' queries.

-- PR-ADS-040A: idempotent migration — enforce NOT NULL on search_terms.search_term
-- New installs: search_term is already NOT NULL from CREATE TABLE above; these are no-ops.
-- Existing DBs (from initial PR-ADS-040 deploy): purge any null/blank rows then
-- set the constraint.  Runs once via the migrations guard.
DO $$
BEGIN
    INSERT INTO migrations (migration_id)
    VALUES ('PR-ADS-040A-search-term-not-null')
    ON CONFLICT (migration_id) DO NOTHING;

    IF FOUND THEN
        DELETE FROM search_terms
        WHERE search_term IS NULL OR BTRIM(search_term) = '';

        ALTER TABLE search_terms ALTER COLUMN search_term SET NOT NULL;
    END IF;
END $$;

-- PR-ADS-044: GCLID attribution persistence — one row per matched GCLID evidence record
-- Stable dedupe key: attribution_key
-- SHA1 of gclid|contact_id|(deal_id or first_url)|campaign_name|keyword|match_status.
-- Multiple deals for the same contact/GCLID are preserved as separate rows.
-- When deal_id is absent, the writer falls back to first_url in the dedupe key.
CREATE TABLE IF NOT EXISTS gclid_attribution (
  id                  SERIAL PRIMARY KEY,

  -- Stable dedupe key generated by the writer (uses deal_id when present, otherwise first_url).
  attribution_key     TEXT NOT NULL UNIQUE,

  run_id              INTEGER REFERENCES runs(id) ON DELETE SET NULL,
  sync_batch_id       INTEGER REFERENCES sync_batches(id) ON DELETE SET NULL,

  gclid               TEXT NOT NULL,
  contact_id          TEXT,
  deal_id             TEXT,

  campaign_name       TEXT,
  keyword             TEXT,
  match_type          TEXT,
  search_term         TEXT,

  company             TEXT,
  country             TEXT,
  first_url           TEXT,

  contact_created_at  TIMESTAMPTZ,
  deal_created_at     TIMESTAMPTZ,
  deal_close_date     TIMESTAMPTZ,

  deal_stage          TEXT,
  deal_stage_label    TEXT,
  deal_amount_usd     NUMERIC(12,2),

  mql_status          TEXT,
  status_category     TEXT,

  match_status        TEXT,  -- matched | unmatched | url_fallback | unknown
  match_source        TEXT,  -- gclid | first_url | crm_field | unknown

  created_at          TIMESTAMPTZ DEFAULT NOW(),
  updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gclid_attr_gclid
  ON gclid_attribution(gclid);

CREATE INDEX IF NOT EXISTS idx_gclid_attr_contact
  ON gclid_attribution(contact_id);

CREATE INDEX IF NOT EXISTS idx_gclid_attr_deal
  ON gclid_attribution(deal_id);

CREATE INDEX IF NOT EXISTS idx_gclid_attr_campaign
  ON gclid_attribution(campaign_name);

CREATE INDEX IF NOT EXISTS idx_gclid_attr_created
  ON gclid_attribution(created_at DESC);

-- Composite cursor index for keyset pagination on (created_at DESC, id DESC)
CREATE INDEX IF NOT EXISTS idx_gclid_attr_cursor
  ON gclid_attribution(created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_gclid_attr_run
  ON gclid_attribution(run_id);

CREATE INDEX IF NOT EXISTS idx_gclid_attr_sync_batch
  ON gclid_attribution(sync_batch_id);

-- PR-ADS-044: GCLID coverage snapshot — one row per run/day capturing aggregate coverage stats
CREATE TABLE IF NOT EXISTS gclid_coverage_snapshots (
  id                      SERIAL PRIMARY KEY,
  run_id                  INTEGER REFERENCES runs(id) ON DELETE SET NULL,
  sync_batch_id           INTEGER REFERENCES sync_batches(id) ON DELETE SET NULL,

  snapshot_date           DATE NOT NULL DEFAULT CURRENT_DATE,

  total_contacts          INTEGER DEFAULT 0,
  contacts_with_gclid     INTEGER DEFAULT 0,
  contacts_without_gclid  INTEGER DEFAULT 0,
  coverage_pct            NUMERIC(6,2),

  total_deals             INTEGER DEFAULT 0,
  matched_deals           INTEGER DEFAULT 0,
  unmatched_deals         INTEGER DEFAULT 0,

  raw_summary             JSONB,

  created_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gclid_coverage_snapshot_date
  ON gclid_coverage_snapshots(snapshot_date DESC);

CREATE INDEX IF NOT EXISTS idx_gclid_coverage_run
  ON gclid_coverage_snapshots(run_id);

-- PR-ADS-029A: idempotent migration — enforce NOT NULL on geo.run_id for existing DBs
-- New installs: run_id is already NOT NULL from CREATE TABLE above; these are no-ops.
-- Existing DBs: removes any orphan rows then sets the constraint (runs once via migrations table).
DO $$
BEGIN
    INSERT INTO migrations (migration_id)
    VALUES ('PR-ADS-029A-geo-run-id-not-null')
    ON CONFLICT (migration_id) DO NOTHING;

    IF FOUND THEN
        DELETE FROM geo WHERE run_id IS NULL;
        ALTER TABLE geo ALTER COLUMN run_id SET NOT NULL;
    END IF;
END $$;

-- PR-ADS-025F: one-time cleanup of pre-merge split rows
-- Safe: next weekly run repopulates with correct merged data
-- REMOVE THIS BLOCK after confirming campaigns table has merged rows
-- with non-zero avg_cpql_usd (verify via GET /api/campaigns?days=30).
-- Owner: Youssef Awwad — tracked in PR-ADS-025F post-deploy checklist.
DO $$
BEGIN
    INSERT INTO migrations (migration_id)
    VALUES ('PR-ADS-025F-truncate-campaigns')
    ON CONFLICT (migration_id) DO NOTHING;

    IF FOUND THEN
        TRUNCATE TABLE campaigns;
    END IF;
END $$;

-- PR-ADS-114: durable Revenue Truth Recovery jobs. Background recovery runs
-- persist their metadata, chunk checkpoints, counts, status, and errors here so
-- progress survives a process restart and resume reads completed chunks from DB.
CREATE TABLE IF NOT EXISTS revenue_recovery_jobs (
  id                SERIAL PRIMARY KEY,
  job_id            TEXT NOT NULL UNIQUE,
  status            TEXT NOT NULL DEFAULT 'queued',  -- queued|running|success|partial|failed
  dry_run           BOOLEAN NOT NULL DEFAULT TRUE,
  date_from         DATE,
  date_to           DATE,
  chunk_months      INTEGER NOT NULL DEFAULT 1,
  phase             TEXT,
  current_chunk     TEXT,
  completed_chunks  JSONB NOT NULL DEFAULT '[]'::jsonb,
  summary           JSONB,
  chunks            JSONB,
  errors            JSONB NOT NULL DEFAULT '[]'::jsonb,
  started_at        TIMESTAMPTZ,
  finished_at       TIMESTAMPTZ,
  created_at        TIMESTAMPTZ DEFAULT NOW(),
  updated_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_revenue_recovery_jobs_created
  ON revenue_recovery_jobs(created_at DESC);
"""


def init_db() -> None:
    """Create all tables and indexes if they do not already exist.

    Idempotent — safe to call on every application startup.
    Non-fatal — logs and returns if the database is unavailable.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                log.warning("init_db skipped — database unavailable")
                return
            with conn.cursor() as cur:
                cur.execute(_DDL)
        log.info("Schema OK — all tables initialised")
    except Exception as exc:  # noqa: BLE001
        log.error("init_db failed: %s", exc)
