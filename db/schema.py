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

  -- Currency lineage (PR-ADS-144): durable raw cost from Google Ads.
  -- cost_micros is the raw metrics.cost_micros from the API;
  -- currency_code is the account native currency (e.g. GBP);
  -- source_system identifies the data origin for provenance auditing.
  cost_micros      BIGINT,
  currency_code    TEXT,
  source_system    TEXT,

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

-- Unique natural key: prevents duplicate fact rows for the same search term event.
-- Includes campaign_id (PR-ADS-144) so two campaign IDs sharing a display name
-- can never collide. COALESCE handles nullable columns; search_term is NOT NULL.
CREATE UNIQUE INDEX IF NOT EXISTS idx_search_terms_unique_fact
  ON search_terms (
    source_date,
    COALESCE(campaign_name,  ''),
    COALESCE(campaign_id,    ''),
    COALESCE(ad_group,       ''),
    COALESCE(keyword,        ''),
    COALESCE(match_type,     ''),
    search_term
  );

-- PR-ADS-144: real idempotent production migration for pre-PR-144 databases.
--
-- A database that predates PR-ADS-144 already has the search_terms table, so the
-- CREATE TABLE IF NOT EXISTS above is a no-op there: the cost_micros /
-- currency_code / source_system columns are NEVER added, and the old
-- idx_search_terms_unique_fact (WITHOUT campaign_id) still exists, so the
-- campaign_id-aware CREATE UNIQUE INDEX IF NOT EXISTS above is ALSO a no-op.
-- This block performs the actual upgrade.
--
-- 1. Add the three lineage columns (ADD COLUMN IF NOT EXISTS is idempotent).
ALTER TABLE search_terms ADD COLUMN IF NOT EXISTS cost_micros   BIGINT;
ALTER TABLE search_terms ADD COLUMN IF NOT EXISTS currency_code TEXT;
ALTER TABLE search_terms ADD COLUMN IF NOT EXISTS source_system TEXT;

-- 2. Deterministically resolve legacy null-campaign_id collisions and swap the
--    unique index to the campaign_id-aware definition. Runs exactly once, guarded
--    by the migrations table.
--
--    Adding campaign_id to the key can only SPLIT groups, never merge them, so the
--    new index can never introduce a NEW collision versus the old key. The ONE
--    real hazard is double-counting: a legacy row with campaign_id NULL and a
--    later Google Ads row bearing an id for the SAME
--    (source_date, campaign_name, ad_group, keyword, match_type, search_term)
--    fact would become two distinct keys. We resolve this deterministically:
--    within each such group, when at least one id-bearing row exists, the
--    NULL-campaign_id row(s) are the ambiguous legacy duplicate of the same fact
--    and are deleted — the precise id-bearing identity wins. Two DISTINCT ids
--    (10 and 20) sharing a display name are NOT touched: both are real, distinct
--    facts and both survive.
DO $$
BEGIN
    INSERT INTO migrations (migration_id)
    VALUES ('PR-ADS-144-currency-and-id-key')
    ON CONFLICT (migration_id) DO NOTHING;

    IF FOUND THEN
        -- Delete ambiguous NULL-campaign_id rows that collide with an id-bearing
        -- row for the same fact (prevents null-id ↔ id-bearing double counting).
        DELETE FROM search_terms st
        WHERE st.campaign_id IS NULL
          AND EXISTS (
              SELECT 1 FROM search_terms other
              WHERE other.campaign_id IS NOT NULL
                AND other.source_date = st.source_date
                AND COALESCE(other.campaign_name, '') = COALESCE(st.campaign_name, '')
                AND COALESCE(other.ad_group,      '') = COALESCE(st.ad_group,      '')
                AND COALESCE(other.keyword,       '') = COALESCE(st.keyword,       '')
                AND COALESCE(other.match_type,    '') = COALESCE(st.match_type,    '')
                AND other.search_term = st.search_term
          );

        -- Swap the index: drop the legacy definition (no campaign_id) and
        -- recreate it with campaign_id included. The recreate cannot fail on a
        -- collision because adding a key column only ever splits groups.
        DROP INDEX IF EXISTS idx_search_terms_unique_fact;
        CREATE UNIQUE INDEX idx_search_terms_unique_fact
          ON search_terms (
            source_date,
            COALESCE(campaign_name, ''),
            COALESCE(campaign_id,   ''),
            COALESCE(ad_group,      ''),
            COALESCE(keyword,       ''),
            COALESCE(match_type,    ''),
            search_term
          );
    END IF;
END $$;

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

-- PR-ADS-146: durable keyword daily fact table (Keyword Evidence).
-- grain / natural key: source_date + customer_id + campaign_id + ad_group_id + criterion_id
-- Immutable Google Ads identity — display names are NEVER the unique key, so two
-- campaigns / ad groups / keyword criteria sharing a display name stay separate
-- facts. A repeated scheduler pull for the same fact updates the SAME row
-- (ON CONFLICT upsert in db/writers.write_keyword_daily_facts).
--
-- The legacy `keywords` snapshot table above is left UNTOUCHED for audit
-- compatibility; its historical amounts are never reinterpreted as durable USD.
-- cost_micros is raw metrics.cost_micros (native currency); currency_code is the
-- native account currency; source_system is provenance. Quality diagnostics are
-- LATEST-OBSERVED keyword attributes (NULL = unavailable, distinct from 0) — they
-- are never averaged across scheduler snapshots.
-- The immutable Google Ads identity columns are NOT NULL: a durable fact with a
-- missing id is REJECTED at the writer (fail closed) and by the DB — we never
-- COALESCE a missing id to '' as a production fallback (that could let two
-- distinct-but-incomplete facts collide). quality_observed_at is the genuine time
-- the quality attributes were observed (pull time), NOT the activity source_date.
CREATE TABLE IF NOT EXISTS keyword_daily_facts (
  id                       SERIAL PRIMARY KEY,
  run_id                   INTEGER REFERENCES runs(id) ON DELETE SET NULL,
  source_date              DATE NOT NULL,
  customer_id              TEXT NOT NULL,
  campaign_id              TEXT NOT NULL,
  campaign_name            TEXT,
  ad_group_id              TEXT NOT NULL,
  ad_group_name            TEXT,
  criterion_id             TEXT NOT NULL,
  keyword_text             TEXT,
  match_type               TEXT,
  criterion_status         TEXT,

  -- Currency lineage: raw native cost + native currency + provenance.
  cost_micros              BIGINT,
  currency_code            TEXT,
  source_system            TEXT,

  impressions              BIGINT        DEFAULT 0,
  clicks                   BIGINT        DEFAULT 0,
  -- conversions is NULLABLE: NULL = platform-conversion evidence unavailable,
  -- distinct from a genuine verified 0. Never coerced to 0 on ingestion.
  conversions              NUMERIC(12,2),

  -- Latest observed Google Ads quality diagnostics (keyword attributes).
  -- NULL = unavailable (never conflated with a genuine 0/score). Not additive.
  -- quality_observed_at stamps WHEN these attributes were observed; latest
  -- quality is chosen by this timestamp, never by the activity source_date.
  quality_score            SMALLINT,
  expected_ctr             TEXT,
  ad_relevance             TEXT,
  landing_page_experience  TEXT,
  quality_observed_at      TIMESTAMPTZ,

  sync_batch_id            INTEGER REFERENCES sync_batches(id) ON DELETE SET NULL,
  created_at               TIMESTAMPTZ DEFAULT NOW(),
  updated_at               TIMESTAMPTZ DEFAULT NOW()
);

-- Unique natural key over the NOT NULL immutable Google Ads identity. No COALESCE
-- fallback — a row missing any id never reaches this index (writer + NOT NULL
-- reject it), so two incomplete facts can never collide.
CREATE UNIQUE INDEX IF NOT EXISTS idx_keyword_daily_facts_unique
  ON keyword_daily_facts (
    source_date, customer_id, campaign_id, ad_group_id, criterion_id
  );

-- Keyset/cursor pagination + lookup indexes.
CREATE INDEX IF NOT EXISTS idx_kdf_cursor      ON keyword_daily_facts(source_date DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_kdf_source_date ON keyword_daily_facts(source_date);
CREATE INDEX IF NOT EXISTS idx_kdf_campaign_id ON keyword_daily_facts(campaign_id);
CREATE INDEX IF NOT EXISTS idx_kdf_criterion   ON keyword_daily_facts(criterion_id);
CREATE INDEX IF NOT EXISTS idx_kdf_match_type  ON keyword_daily_facts(match_type);
CREATE INDEX IF NOT EXISTS idx_kdf_sync_batch  ON keyword_daily_facts(sync_batch_id);

-- Register the migration marker. keyword_daily_facts is a NEW table, so
-- CREATE TABLE / CREATE INDEX IF NOT EXISTS above are inherently idempotent and
-- non-destructive on databases that predate PR-ADS-146; this marker records the
-- upgrade for audit parity with the search_terms migration.
DO $$
BEGIN
    INSERT INTO migrations (migration_id)
    VALUES ('PR-ADS-146-keyword-daily-facts')
    ON CONFLICT (migration_id) DO NOTHING;
END $$;

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
  job_type          TEXT NOT NULL DEFAULT 'revenue_recovery',  -- revenue_recovery|lead_reconciliation
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

-- Existing installs: add job_type so the durable-job table is reused for the
-- PR-ADS-115 lead-reconciliation job (idempotent).
ALTER TABLE revenue_recovery_jobs
  ADD COLUMN IF NOT EXISTS job_type TEXT NOT NULL DEFAULT 'revenue_recovery';

CREATE INDEX IF NOT EXISTS idx_revenue_recovery_jobs_created
  ON revenue_recovery_jobs(created_at DESC);

-- PR-ADS-115: durable lead-truth exclusions. A missing-event-date paid lead with
-- no verifiable HubSpot identity / created date is excluded from revenue-truth
-- metrics with an explicit, auditable reason. Historical `leads` rows are NEVER
-- overwritten or deleted; exclusion is a separate, reversible truth decision.
CREATE TABLE IF NOT EXISTS lead_truth_exclusions (
  id                     SERIAL PRIMARY KEY,
  lead_id                TEXT NOT NULL UNIQUE,  -- COALESCE(contact_id, 'id:'||leads.id)
  reason                 TEXT NOT NULL,         -- no_contact_identity|hubspot_contact_not_found|hubspot_contact_no_createdate
  details                TEXT,
  reconciliation_job_id  TEXT,
  excluded_at            TIMESTAMPTZ DEFAULT NOW(),
  updated_at             TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lead_truth_exclusions_reason
  ON lead_truth_exclusions(reason);

-- PR-ADS-117: durable acquisition-source classification of HubSpot contacts.
-- Raw HubSpot source values are persisted alongside the derived group, rule
-- version, and timestamp so every classification is auditable. Raw HubSpot data
-- is NEVER overwritten or deleted.
CREATE TABLE IF NOT EXISTS contact_source_classification (
  id                        SERIAL PRIMARY KEY,
  contact_key               TEXT NOT NULL UNIQUE,   -- contact_id, or 'id:'||leads.id
  contact_id                TEXT,
  source_primary_raw        TEXT,
  source_detail_raw         TEXT,
  acquisition_group         TEXT NOT NULL,          -- google_ads|other_paid|organic|offline|unclassified
  classification_rule_version TEXT NOT NULL,
  contact_created_at        TIMESTAMPTZ,
  status_category           TEXT,
  classified_at             TIMESTAMPTZ DEFAULT NOW(),
  updated_at                TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_contact_source_group
  ON contact_source_classification(acquisition_group);
CREATE INDEX IF NOT EXISTS idx_contact_source_created
  ON contact_source_classification(contact_created_at);

-- PR-ADS-117: durable per-deal source attribution. Revenue is NEVER split across
-- sources — each closed-won deal maps to exactly one group (or the ambiguous /
-- unclassified bucket).
CREATE TABLE IF NOT EXISTS deal_source_attribution (
  id                     SERIAL PRIMARY KEY,
  deal_id                TEXT NOT NULL UNIQUE,
  associated_contact_id  TEXT,
  acquisition_group      TEXT NOT NULL,             -- group | 'ambiguous' | 'unclassified'
  source_primary_raw     TEXT,
  source_detail_raw      TEXT,
  attribution_status     TEXT NOT NULL,             -- attributed|ambiguous|unclassified
  attribution_reason     TEXT,
  deal_close_date        TIMESTAMPTZ,
  deal_amount_usd        NUMERIC(12,2),
  classification_rule_version TEXT,
  classified_at          TIMESTAMPTZ DEFAULT NOW(),
  updated_at             TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_deal_source_group
  ON deal_source_attribution(acquisition_group);
CREATE INDEX IF NOT EXISTS idx_deal_source_close
  ON deal_source_attribution(deal_close_date);

-- PR-ADS-118: canonical Google Ads campaign-daily spend — the spend-truth fact
-- read DIRECTLY from the Google Ads API (not derived from the geo table). Raw
-- cost_micros is preserved; the normalised amount is stored for convenience but
-- aggregation always sums micros first. Unique per (customer, campaign, day).
CREATE TABLE IF NOT EXISTS google_ads_campaign_daily_spend (
  id                      SERIAL PRIMARY KEY,
  customer_id             TEXT NOT NULL,
  currency_code           TEXT,
  campaign_id             TEXT NOT NULL,
  campaign_name           TEXT,
  spend_date              DATE NOT NULL,
  cost_micros             BIGINT NOT NULL DEFAULT 0,
  spend_account_currency  NUMERIC(18,6) NOT NULL DEFAULT 0,
  sync_run_id             TEXT,
  source_query_version    TEXT,
  fetched_at              TIMESTAMPTZ DEFAULT NOW(),
  updated_at              TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (customer_id, campaign_id, spend_date)
);

CREATE INDEX IF NOT EXISTS idx_ga_daily_spend_date
  ON google_ads_campaign_daily_spend(spend_date);
CREATE INDEX IF NOT EXISTS idx_ga_daily_spend_campaign
  ON google_ads_campaign_daily_spend(campaign_id);

-- PR-ADS-118: per-chunk fetch ledger so the audit can distinguish a genuinely
-- zero-spend day INSIDE a successfully fetched chunk from a date range that was
-- never fetched. A missing chunk is NEVER treated as zero spend.
CREATE TABLE IF NOT EXISTS google_ads_spend_coverage (
  id                    SERIAL PRIMARY KEY,
  customer_id           TEXT NOT NULL,
  chunk_start           DATE NOT NULL,
  chunk_end             DATE NOT NULL,
  status                TEXT NOT NULL,          -- verified | failed
  rows_written          INTEGER NOT NULL DEFAULT 0,
  cost_micros_total     BIGINT NOT NULL DEFAULT 0,
  source_query_version  TEXT,
  sync_run_id           TEXT,
  fetched_at            TIMESTAMPTZ DEFAULT NOW(),
  updated_at            TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (customer_id, chunk_start, chunk_end)
);

CREATE INDEX IF NOT EXISTS idx_ga_spend_coverage_range
  ON google_ads_spend_coverage(chunk_start, chunk_end);

-- PR-ADS-120: account-level daily spend, persisted separately from campaign rows
-- so the campaign-daily sum can be reconciled against the direct account total.
-- account_time_zone is the Google Ads account local zone — spend windows use the
-- account's local day, not server time / UTC.
CREATE TABLE IF NOT EXISTS google_ads_account_daily_spend (
  id                 SERIAL PRIMARY KEY,
  customer_id        TEXT NOT NULL,
  spend_date         DATE NOT NULL,
  cost_micros        BIGINT NOT NULL DEFAULT 0,
  currency_code      TEXT,
  account_time_zone  TEXT,
  sync_run_id        TEXT,
  source_query_version TEXT,
  fetched_at         TIMESTAMPTZ DEFAULT NOW(),
  updated_at         TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (customer_id, spend_date)
);

CREATE INDEX IF NOT EXISTS idx_ga_account_daily_spend_date
  ON google_ads_account_daily_spend(spend_date);

-- PR-ADS-124: canonical Google Ads geo (country) daily spend, read DIRECTLY from
-- the Google Ads API geographic_view (not the legacy run-scoped `geo` table or
-- Windsor). This is the geo spend-truth used to reconcile country-level spend
-- against the canonical campaign-level total so Country ROAS can be trusted.
-- Raw cost_micros is preserved; unique per (customer, country, campaign, day).
-- country_code / country_name resolve the criterion id via geo_target_constant so
-- the canonical geo table itself feeds named ROAS by Country rows (the same source
-- as the reconciliation total) — never a different spend source.
CREATE TABLE IF NOT EXISTS google_ads_geo_daily_spend (
  id                    SERIAL PRIMARY KEY,
  customer_id           TEXT NOT NULL,
  currency_code         TEXT,
  country_criterion_id  TEXT NOT NULL DEFAULT '',
  country_code          TEXT,
  country_name          TEXT,
  campaign_id           TEXT NOT NULL DEFAULT '',
  spend_date            DATE NOT NULL,
  cost_micros           BIGINT NOT NULL DEFAULT 0,
  sync_run_id           TEXT,
  source_query_version  TEXT,
  fetched_at            TIMESTAMPTZ DEFAULT NOW(),
  updated_at            TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (customer_id, country_criterion_id, campaign_id, spend_date)
);

CREATE INDEX IF NOT EXISTS idx_ga_geo_daily_spend_date
  ON google_ads_geo_daily_spend(spend_date);
CREATE INDEX IF NOT EXISTS idx_ga_geo_daily_spend_country
  ON google_ads_geo_daily_spend(country_criterion_id);

-- Existing DBs: add the country resolution columns if the table predates them.
ALTER TABLE google_ads_geo_daily_spend ADD COLUMN IF NOT EXISTS country_code TEXT;
ALTER TABLE google_ads_geo_daily_spend ADD COLUMN IF NOT EXISTS country_name TEXT;

-- PR-ADS-119: durable daily FX rates. Google Ads native spend (GBP) is converted
-- to USD reporting spend using the rate for each spend row's OWN spend_date — never
-- a single current spot rate for a whole quarter. A missing rate_date makes FX
-- coverage incomplete and blocks ROAS (never silently converts at a wrong rate).
CREATE TABLE IF NOT EXISTS fx_rates (
  id              SERIAL PRIMARY KEY,
  rate_date       DATE NOT NULL,
  base_currency   TEXT NOT NULL,
  quote_currency  TEXT NOT NULL,
  rate            NUMERIC(18,8) NOT NULL,
  provider        TEXT,
  fetched_at      TIMESTAMPTZ DEFAULT NOW(),
  source_version  TEXT,
  updated_at      TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (rate_date, base_currency, quote_currency)
);

CREATE INDEX IF NOT EXISTS idx_fx_rates_lookup
  ON fx_rates(base_currency, quote_currency, rate_date);

-- PR-ADS-119: durable campaign identity mapping. The raw Google Ads campaign
-- identity (campaign_id + historical_campaign_name) is immutable truth; this
-- table records how an external HubSpot/UTM label maps to a canonical campaign.
-- Manual mappings are explicit and auditable (approved_at) and NEVER overwrite
-- the raw campaign identity. Exact normalized matches may be auto-linked; fuzzy
-- matches (e.g. "mexico,chile" -> "Emerging Markets") are NEVER auto-applied.
CREATE TABLE IF NOT EXISTS google_ads_campaign_identity (
  id                       SERIAL PRIMARY KEY,
  customer_id              TEXT NOT NULL,
  campaign_id              TEXT,
  canonical_campaign_name  TEXT,
  historical_campaign_name TEXT,
  external_campaign_label  TEXT NOT NULL,
  match_method             TEXT NOT NULL,         -- exact_normalized | manual | not_google_ads | unmatched
  approved_at              TIMESTAMPTZ,
  approved_by              TEXT,
  created_at               TIMESTAMPTZ DEFAULT NOW(),
  updated_at               TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (customer_id, external_campaign_label)
);

CREATE INDEX IF NOT EXISTS idx_ga_campaign_identity_label
  ON google_ads_campaign_identity(external_campaign_label);
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
