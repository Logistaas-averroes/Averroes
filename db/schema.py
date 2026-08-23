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
    -- PR-ADS-154A: widened from VARCHAR(20). The scheduler's canonical run type
    -- is `daily_incremental_sync` — 22 characters — so every incremental run
    -- failed at INSERT with "value too long for type character varying(20)".
    -- The mismatch predates PR-ADS-154; that PR surfaced it by finally
    -- initializing the pool and requiring a durable run record before any
    -- external pull, which turned a silent no-op into a loud failure.
    --
    -- 64 leaves room for descriptive machine identifiers rather than forcing
    -- future run types to be abbreviated to fit a column. The value is what
    -- scheduler output, tests, monitoring and diagnostics already key on, so
    -- the column moves to the contract, not the contract to the column.
    run_type            VARCHAR(64)  NOT NULL,
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

-- PR-ADS-146A: DB-backed lease so only one worker/process owns a resumable job
-- (e.g. keyword_bootstrap) at a time, and a stale lease (crashed Render worker)
-- can be detected and recovered. All idempotent.
ALTER TABLE revenue_recovery_jobs
  ADD COLUMN IF NOT EXISTS lease_token      TEXT;
ALTER TABLE revenue_recovery_jobs
  ADD COLUMN IF NOT EXISTS heartbeat_at     TIMESTAMPTZ;
ALTER TABLE revenue_recovery_jobs
  ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
ALTER TABLE revenue_recovery_jobs
  ADD COLUMN IF NOT EXISTS last_progress_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_revenue_recovery_jobs_created
  ON revenue_recovery_jobs(created_at DESC);

-- At most one RUNNING keyword_bootstrap job at a time — the atomic claim that
-- makes the DB lease authoritative even across processes (a cold-start race
-- resolves to a single winner via this partial unique index). Scoped to
-- keyword_bootstrap so existing job types are unaffected.
CREATE UNIQUE INDEX IF NOT EXISTS uq_recovery_running_keyword_bootstrap
  ON revenue_recovery_jobs (job_type)
  WHERE status = 'running' AND job_type = 'keyword_bootstrap';

-- PR-ADS-151 §5: at most one RUNNING mailchimp_backfill job at a time — the
-- atomic claim that makes the durable Mailchimp backfill lease authoritative
-- across processes/deploys (reuses the same recovery-job lease machinery).
CREATE UNIQUE INDEX IF NOT EXISTS uq_recovery_running_mailchimp_backfill
  ON revenue_recovery_jobs (job_type)
  WHERE status = 'running' AND job_type = 'mailchimp_backfill';

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

-- PR-ADS-151: Mailchimp read-only email-marketing evidence foundation.
-- Additive, durable, natural-key tables. Immutable Mailchimp campaign IDs are
-- the natural key so a repeated sync UPDATES the same row (never duplicates).
-- Campaign totals are stored ONE ROW PER CAMPAIGN (upserted), never as repeated
-- scheduler snapshots that could later be summed together accidentally.
-- Mailchimp is PULL ONLY — nothing here is ever written back to Mailchimp.

-- One row per campaign (immutable campaign_id = natural key).
CREATE TABLE IF NOT EXISTS mailchimp_campaigns (
  id               SERIAL PRIMARY KEY,
  campaign_id      TEXT NOT NULL UNIQUE,       -- immutable Mailchimp id (natural key)
  web_id           BIGINT,
  list_id          TEXT,                       -- audience/list id
  campaign_type    TEXT,
  status           TEXT,
  subject_line     TEXT,
  title            TEXT,
  create_time      TIMESTAMPTZ,
  send_time        TIMESTAMPTZ,
  emails_sent      INTEGER,
  recipient_count  INTEGER,
  source_system    TEXT,                       -- source provenance
  sync_batch_id    INTEGER REFERENCES sync_batches(id) ON DELETE SET NULL,
  first_seen_at    TIMESTAMPTZ DEFAULT NOW(),
  updated_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mc_campaigns_list       ON mailchimp_campaigns(list_id);
CREATE INDEX IF NOT EXISTS idx_mc_campaigns_send_time  ON mailchimp_campaigns(send_time DESC);
CREATE INDEX IF NOT EXISTS idx_mc_campaigns_status     ON mailchimp_campaigns(status);
CREATE INDEX IF NOT EXISTS idx_mc_campaigns_sync_batch ON mailchimp_campaigns(sync_batch_id);

-- One row per campaign report (campaign_id = natural key). Metrics may keep
-- changing after send, so the row is UPDATED in place on each refresh — this is
-- current-state truth, NOT an additive snapshot ledger.
CREATE TABLE IF NOT EXISTS mailchimp_campaign_reports (
  id                       SERIAL PRIMARY KEY,
  campaign_id              TEXT NOT NULL UNIQUE,   -- natural key
  list_id                  TEXT,
  campaign_type            TEXT,
  subject_line             TEXT,
  title                    TEXT,
  send_time                TIMESTAMPTZ,
  emails_sent              INTEGER,
  delivered_estimate       INTEGER,               -- emails_sent − hard − soft − syntax
  opens_total              INTEGER,
  unique_opens             INTEGER,
  open_rate                NUMERIC(8,5),
  clicks_total             INTEGER,
  unique_clicks            INTEGER,
  unique_subscriber_clicks INTEGER,
  click_rate               NUMERIC(8,5),
  hard_bounces             INTEGER,
  soft_bounces             INTEGER,
  syntax_errors            INTEGER,
  unsubscribes             INTEGER,
  abuse_reports            INTEGER,
  forwards_count           INTEGER,
  last_open                TIMESTAMPTZ,
  last_click               TIMESTAMPTZ,
  last_report_update       TIMESTAMPTZ,           -- when we last refreshed this report
  source_system            TEXT,
  sync_batch_id            INTEGER REFERENCES sync_batches(id) ON DELETE SET NULL,
  first_seen_at            TIMESTAMPTZ DEFAULT NOW(),
  updated_at               TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mc_reports_list        ON mailchimp_campaign_reports(list_id);
CREATE INDEX IF NOT EXISTS idx_mc_reports_send_time   ON mailchimp_campaign_reports(send_time DESC);
CREATE INDEX IF NOT EXISTS idx_mc_reports_updated     ON mailchimp_campaign_reports(last_report_update DESC);
CREATE INDEX IF NOT EXISTS idx_mc_reports_sync_batch  ON mailchimp_campaign_reports(sync_batch_id);

-- One row per (campaign_id, link_id) — aggregate click metrics per link/URL.
CREATE TABLE IF NOT EXISTS mailchimp_campaign_links (
  id                       SERIAL PRIMARY KEY,
  campaign_id              TEXT NOT NULL,
  link_id                  TEXT NOT NULL,
  url                      TEXT,
  total_clicks             INTEGER,
  unique_clicks            INTEGER,
  click_percentage         NUMERIC(8,5),
  unique_click_percentage  NUMERIC(8,5),
  source_system            TEXT,
  sync_batch_id            INTEGER REFERENCES sync_batches(id) ON DELETE SET NULL,
  first_seen_at            TIMESTAMPTZ DEFAULT NOW(),
  updated_at               TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (campaign_id, link_id)
);

CREATE INDEX IF NOT EXISTS idx_mc_links_campaign   ON mailchimp_campaign_links(campaign_id);
CREATE INDEX IF NOT EXISTS idx_mc_links_sync_batch ON mailchimp_campaign_links(sync_batch_id);

-- Point-in-time audience state snapshots (list_id + snapshot_date = natural key).
-- Audiences legitimately change over time, so these ARE dated snapshots — but the
-- unique key prevents two rows for the same list+day, so a re-sync overwrites the
-- day rather than accumulating summable duplicates.
CREATE TABLE IF NOT EXISTS mailchimp_audience_snapshots (
  id                   SERIAL PRIMARY KEY,
  list_id              TEXT NOT NULL,
  snapshot_date        DATE NOT NULL,
  list_name            TEXT,
  date_created         TIMESTAMPTZ,
  member_count         INTEGER,
  unsubscribe_count    INTEGER,
  cleaned_count        INTEGER,
  pending_count        INTEGER,
  member_count_since_send INTEGER,
  open_rate            NUMERIC(8,5),
  click_rate           NUMERIC(8,5),
  campaign_count       INTEGER,
  source_system        TEXT,
  sync_batch_id        INTEGER REFERENCES sync_batches(id) ON DELETE SET NULL,
  created_at           TIMESTAMPTZ DEFAULT NOW(),
  updated_at           TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (list_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_mc_audience_list ON mailchimp_audience_snapshots(list_id);
CREATE INDEX IF NOT EXISTS idx_mc_audience_date ON mailchimp_audience_snapshots(snapshot_date DESC);

-- Mailchimp-specific sync state — backfill completeness + rolling-refresh
-- watermarks per scope (e.g. 'campaigns'). Distinct from the generic sync_state
-- table (which drives Dataset Freshness); this tracks the historical-backfill
-- lifecycle and the rolling report-refresh window.
CREATE TABLE IF NOT EXISTS mailchimp_sync_state (
  id                     SERIAL PRIMARY KEY,
  scope                  TEXT NOT NULL UNIQUE,   -- 'campaigns'
  backfill_status        TEXT NOT NULL DEFAULT 'not_started',  -- not_started|running|partial|complete|failed
  backfill_started_at    TIMESTAMPTZ,
  backfill_completed_at  TIMESTAMPTZ,
  last_incremental_at    TIMESTAMPTZ,
  earliest_send_time     TIMESTAMPTZ,
  latest_send_time       TIMESTAMPTZ,
  campaigns_seen         INTEGER DEFAULT 0,
  reports_refreshed      INTEGER DEFAULT 0,
  last_batch_id          INTEGER REFERENCES sync_batches(id) ON DELETE SET NULL,
  last_error             TEXT,
  updated_at             TIMESTAMPTZ DEFAULT NOW()
);

-- ===========================================================================
-- PR-ADS-153B — CANONICAL CRM FUNNEL TRUTH
-- ===========================================================================
-- HubSpot Lifecycle Stage is the canonical Averroes funnel spine. This table is
-- the durable ALL-SOURCE latest-state contact store: one row per HubSpot contact
-- id, refreshed by modification watermark (`lastmodifieddate`), never by contact
-- creation recency. It exists alongside — and does NOT replace — the legacy
-- `leads` snapshot table, whose historical semantics are paid-search lead
-- evidence and which PR-ADS-153C will migrate off.
--
-- Truth rules baked into this shape:
--   * `contact_id` is the durable HubSpot identity and the ONLY dedup key.
--   * Stage-entry timestamps are persisted per stage so a contact currently at
--     `customer` REMAINS countable in the historical MQL / SQL cohorts. Funnel
--     counts are never made mutually exclusive by current lifecycle stage.
--   * A NULL stage-entry column means "HubSpot supplied no evidence" — it is a
--     coverage gap, NEVER silently substituted with createdate.
--   * `mql_status` holds ONLY the real HubSpot property. Free text (MDR
--     comments) may never be written here; historical pollution is detected and
--     reported, never rewritten in place.
--   * `lifecycle_stage` preserves every live value verbatim, including
--     subscriber / evangelist / other and the custom Discarded-Contact and
--     Reseller stages. Unknown new stages are preserved, never guessed.
--   * No email address is stored. Identity is the HubSpot contact id.
CREATE TABLE IF NOT EXISTS hubspot_contact_funnel (
  id                        SERIAL PRIMARY KEY,
  contact_id                TEXT NOT NULL UNIQUE,   -- durable HubSpot identity
  created_at                TIMESTAMPTZ,            -- HubSpot createdate
  last_modified_at          TIMESTAMPTZ,            -- HubSpot lastmodifieddate (sync watermark)

  -- Canonical lifecycle truth
  lifecycle_stage           TEXT,                   -- raw HubSpot lifecyclestage, normalised case only
  lead_status               TEXT,                   -- hs_lead_status
  mql_status                TEXT,                   -- HubSpot mql_status ONLY — never MDR free text
  mql_status_category       TEXT,                   -- operational category (analysis/mql_status_taxonomy)

  -- Canonical stage-entry event dates (NULL = no HubSpot evidence, not "createdate")
  date_entered_lead         TIMESTAMPTZ,
  date_entered_mql          TIMESTAMPTZ,
  date_entered_sql          TIMESTAMPTZ,
  date_entered_opportunity  TIMESTAMPTZ,
  date_entered_customer     TIMESTAMPTZ,
  -- Newest stage-entry timestamp on this row (max of the five above). Derived in
  -- the same write from the same evidence — never a cache that can drift — so
  -- the `hubspot/lifecycle_events` dataset has a real recency column.
  latest_stage_entry_at     TIMESTAMPTZ,

  -- Acquisition-source evidence (feeds the existing source/campaign/keyword
  -- attribution doctrine — raw values only, never derived from campaign name)
  hs_analytics_source       TEXT,
  hs_analytics_source_data_1 TEXT,                  -- campaign label
  hs_analytics_source_data_2 TEXT,                  -- keyword label
  hs_latest_source          TEXT,
  hs_latest_source_data_1   TEXT,
  hs_latest_source_data_2   TEXT,
  hs_analytics_first_url    TEXT,

  -- Geography + firmographics used by existing country/source attribution
  ip_country                TEXT,
  country                   TEXT,
  company                   TEXT,
  owner_id                  TEXT,

  -- GCLID evidence, same privacy doctrine as `leads` (no synthetic GCLIDs)
  gclid                     TEXT,
  has_gclid                 BOOLEAN DEFAULT FALSE,

  -- Provenance / lineage
  source_system             TEXT DEFAULT 'hubspot_api',
  lifecycle_rule_version    TEXT,
  mql_rule_version          TEXT,
  first_ingested_at         TIMESTAMPTZ DEFAULT NOW(),
  last_ingested_at          TIMESTAMPTZ DEFAULT NOW(),
  sync_batch_id             INTEGER REFERENCES sync_batches(id) ON DELETE SET NULL,
  updated_at                TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_hcf_lifecycle_stage ON hubspot_contact_funnel(lifecycle_stage);
CREATE INDEX IF NOT EXISTS idx_hcf_last_modified   ON hubspot_contact_funnel(last_modified_at DESC);
CREATE INDEX IF NOT EXISTS idx_hcf_created         ON hubspot_contact_funnel(created_at);
CREATE INDEX IF NOT EXISTS idx_hcf_source          ON hubspot_contact_funnel(hs_analytics_source);
CREATE INDEX IF NOT EXISTS idx_hcf_entered_lead    ON hubspot_contact_funnel(date_entered_lead);
CREATE INDEX IF NOT EXISTS idx_hcf_entered_mql     ON hubspot_contact_funnel(date_entered_mql);
CREATE INDEX IF NOT EXISTS idx_hcf_entered_sql     ON hubspot_contact_funnel(date_entered_sql);
CREATE INDEX IF NOT EXISTS idx_hcf_entered_opp     ON hubspot_contact_funnel(date_entered_opportunity);
CREATE INDEX IF NOT EXISTS idx_hcf_entered_cust    ON hubspot_contact_funnel(date_entered_customer);
CREATE INDEX IF NOT EXISTS idx_hcf_mql_category    ON hubspot_contact_funnel(mql_status_category);
CREATE INDEX IF NOT EXISTS idx_hcf_latest_stage    ON hubspot_contact_funnel(latest_stage_entry_at DESC);

-- Durable bootstrap + incremental watermark for the canonical contact sync.
-- ONE ingestion service owns this table (services/hubspot_contact_funnel_sync_service).
-- Completion state is never held in process memory: a restarted worker resumes
-- from `last_modified_watermark`, and bootstrap completeness is explicit rather
-- than inferred from the presence of recent rows.
CREATE TABLE IF NOT EXISTS hubspot_contact_funnel_sync_state (
  id                        SERIAL PRIMARY KEY,
  scope                     TEXT NOT NULL UNIQUE,   -- 'contacts'
  bootstrap_status          TEXT NOT NULL DEFAULT 'not_started', -- not_started|running|partial|complete|failed
  bootstrap_started_at      TIMESTAMPTZ,
  bootstrap_completed_at    TIMESTAMPTZ,
  -- Exclusive-ish resume point: contacts modified at/after this instant are
  -- re-fetched on the next run (an explicit overlap is applied by the service).
  last_modified_watermark   TIMESTAMPTZ,
  last_incremental_at       TIMESTAMPTZ,
  earliest_created_at       TIMESTAMPTZ,
  latest_modified_at        TIMESTAMPTZ,
  contacts_seen             INTEGER DEFAULT 0,
  pages_fetched             INTEGER DEFAULT 0,
  last_batch_id             INTEGER REFERENCES sync_batches(id) ON DELETE SET NULL,
  last_error                TEXT,
  updated_at                TIMESTAMPTZ DEFAULT NOW()
);

-- PR-ADS-153D: durable LOCAL review decisions for canonical search terms.
--
-- One row per durable search-term identity (analysis/search_term_identity.py):
-- canonical campaign identity + normalized search term. Search Terms and the
-- Action Queue read the SAME row, so a decision made on one surface is
-- immediately true on the other — there is no second review-state system.
--
-- This table is a DECISION/ANNOTATION layer. It holds no spend, clicks or
-- impressions and must never become a second Google Ads fact ledger: canonical
-- metrics always come from `search_terms` (PR-ADS-153D §23).
--
-- `review_state = 'exclude_candidate'` is a LOCAL RECOMMENDATION ONLY. It is
-- NOT evidence that a Google Ads negative keyword was applied — this system has
-- no write path to Google Ads (§15, §16).
--
-- History is preserved: `first_flagged_at` / `latest_flagged_at` survive a
-- resolution, so a term that no longer meets the current rule is still auditable
-- as historically flagged (§25).
CREATE TABLE IF NOT EXISTS search_term_review (
  id                      SERIAL PRIMARY KEY,
  term_identity           TEXT NOT NULL UNIQUE,   -- sha256 digest of the pair below
  campaign_key            TEXT NOT NULL,          -- canonical campaign identity
  search_term_normalized  TEXT NOT NULL,          -- normalized user query
  search_term_display     TEXT,                   -- last-seen raw query, for humans
  campaign_name_display   TEXT,                   -- last-seen campaign label
  identity_rule_version   TEXT NOT NULL,

  -- unreviewed | keep | monitor | exclude_candidate | resolved
  review_state            TEXT NOT NULL DEFAULT 'unreviewed',
  review_note             TEXT,
  reviewed_by             TEXT,
  reviewed_at             TIMESTAMPTZ,

  -- Flag history (audit): set by the flagged-view writer, never cleared.
  first_flagged_at        TIMESTAMPTZ,
  latest_flagged_at       TIMESTAMPTZ,
  latest_flag_reason      TEXT,                   -- canonical waste-reason id
  latest_raw_reason       TEXT,                   -- raw junk_category, preserved

  created_at              TIMESTAMPTZ DEFAULT NOW(),
  updated_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_search_term_review_state
  ON search_term_review(review_state);
CREATE INDEX IF NOT EXISTS idx_search_term_review_campaign
  ON search_term_review(campaign_key);
CREATE INDEX IF NOT EXISTS idx_search_term_review_flagged
  ON search_term_review(latest_flagged_at DESC);

-- ══════════════════════════════════════════════════════════════════════════
-- PR-ADS-153E-A — CANONICAL DEAL LEDGER (additive, shadow mode)
-- ══════════════════════════════════════════════════════════════════════════
-- The ONE revenue population. Every synced HubSpot deal lands here exactly
-- once, keyed by `deal_id` — the durable HubSpot identity — regardless of
-- whether it has a GCLID, a campaign mapping, an associated contact, a country
-- or a classified acquisition source.
--
-- Why this table exists (PR-ADS-153A §9.2): revenue was split across three
-- incompatible lineages. `gclid_attribution` keys on a SHA1 attribution hash,
-- not the deal, and structurally excludes non-GCLID revenue.
-- `deal_source_attribution` is deal-keyed but carries no lifecycle or currency
-- contract. A local JSON chain feeds Unit Economics with no dedup at all. Two
-- pages could therefore report different customer and revenue totals for the
-- same window, by construction.
--
-- Doctrine encoded here:
--   * WON is `hs_is_closed_won` — the authoritative HubSpot boolean. Stage
--     labels are display evidence and must NEVER decide whether a deal is won.
--     A missing boolean fails CLOSED (not won).
--   * ATTRIBUTION is nullable EVIDENCE. Its absence never removes a deal from
--     all-source revenue truth.
--   * CURRENCY is fail-closed. `revenue_usd` is populated only when the
--     currency is proven; otherwise it stays NULL with an explicit status and
--     reason. Never coerced to zero, never silently assumed USD.
--   * ALL relevant pipeline stages are stored (open / lost / downgrade /
--     churn), so open pipeline is visible and churn evidence is never erased.
--
-- SHADOW MODE: this ledger is populated and reconciled here but consumes
-- nothing. `gclid_attribution`, `deal_source_attribution` and the legacy
-- `deals` snapshot are deliberately left intact as comparison sources until
-- PR-ADS-153E-B migrates consumers and PR-ADS-153G retires them.
CREATE TABLE IF NOT EXISTS hubspot_deal_ledger (
  -- Durable HubSpot identity. NOT a hash, NOT a contact, NOT a GCLID.
  deal_id                  TEXT PRIMARY KEY,
  deal_name                TEXT,

  -- Lifecycle / stage evidence (display only — never the won predicate).
  pipeline_id              TEXT,
  deal_stage_id            TEXT,
  deal_stage_label         TEXT,
  hs_is_closed             BOOLEAN,
  hs_is_closed_won         BOOLEAN,           -- THE canonical won predicate

  deal_created_at          TIMESTAMPTZ,
  deal_close_date          TIMESTAMPTZ,
  hubspot_lastmodified_at  TIMESTAMPTZ,       -- drives incremental sync + replay guard

  -- Currency lineage, persisted separately so a USD claim is always provable.
  amount_raw               NUMERIC(18,2),     -- deal amount in its own currency
  deal_currency_code       TEXT,
  amount_in_home_currency  NUMERIC(18,2),     -- HubSpot portal home currency
  home_currency_code       TEXT,              -- VERIFIED home currency, if known
  revenue_usd              NUMERIC(18,2),     -- NULL unless currency is proven
  currency_status          TEXT,              -- verified_usd|converted|unavailable|...
  currency_reason          TEXT,

  -- Association evidence (resolved by the ONE shared resolver).
  primary_contact_id       TEXT,
  association_count        INTEGER,
  association_status       TEXT,              -- resolved|ambiguous|none|lookup_failed
  association_reason       TEXT,

  -- Attribution evidence — nullable by design.
  gclid                    TEXT,
  campaign_name_raw        TEXT,
  keyword_raw              TEXT,
  country_raw              TEXT,
  source_primary_raw       TEXT,
  source_detail_raw        TEXT,
  acquisition_group        TEXT,
  attribution_status       TEXT,              -- attributed|ambiguous|unclassified|unavailable
  attribution_reason       TEXT,

  sync_batch_id            INTEGER REFERENCES sync_batches(id) ON DELETE SET NULL,
  source_fetched_at        TIMESTAMPTZ,
  created_at               TIMESTAMPTZ DEFAULT NOW(),
  updated_at               TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_deal_ledger_close_date
  ON hubspot_deal_ledger(deal_close_date);
CREATE INDEX IF NOT EXISTS idx_deal_ledger_lastmodified
  ON hubspot_deal_ledger(hubspot_lastmodified_at DESC);
CREATE INDEX IF NOT EXISTS idx_deal_ledger_stage
  ON hubspot_deal_ledger(deal_stage_id);
CREATE INDEX IF NOT EXISTS idx_deal_ledger_won
  ON hubspot_deal_ledger(hs_is_closed_won);
CREATE INDEX IF NOT EXISTS idx_deal_ledger_campaign
  ON hubspot_deal_ledger(campaign_name_raw);
CREATE INDEX IF NOT EXISTS idx_deal_ledger_acquisition_group
  ON hubspot_deal_ledger(acquisition_group);
CREATE INDEX IF NOT EXISTS idx_deal_ledger_currency_status
  ON hubspot_deal_ledger(currency_status);

-- Deal -> contact association bridge. EVERY association is retained, including
-- for deals whose primary contact could not be chosen: an ambiguous deal keeps
-- all of its candidates so a human can see exactly why it is ambiguous.
--
-- Association evidence is NEVER deleted or replaced by an incomplete or failed
-- HubSpot lookup (PR-ADS-153E-A §4/§6). A failed lookup marks the SYNC attempt
-- incomplete and leaves the last successful observation standing — losing
-- attribution because an API call timed out would silently move revenue between
-- sources.
CREATE TABLE IF NOT EXISTS hubspot_deal_contact_association (
  id                       SERIAL PRIMARY KEY,
  deal_id                  TEXT NOT NULL,
  contact_id               TEXT NOT NULL,

  association_type_id      TEXT,
  association_label        TEXT,

  -- Was this contact selected as the deal's primary, and why.
  is_primary               BOOLEAN DEFAULT FALSE,
  primary_selection_reason TEXT,

  -- Per-contact attribution evidence, kept so a conflict is explainable.
  gclid                    TEXT,
  campaign_name_raw        TEXT,
  keyword_raw              TEXT,
  country_raw              TEXT,
  source_primary_raw       TEXT,
  source_detail_raw        TEXT,
  acquisition_group        TEXT,

  -- Last batch in which this association was SUCCESSFULLY observed.
  last_observed_batch_id   INTEGER REFERENCES sync_batches(id) ON DELETE SET NULL,
  last_observed_at         TIMESTAMPTZ,
  created_at               TIMESTAMPTZ DEFAULT NOW(),
  updated_at               TIMESTAMPTZ DEFAULT NOW(),

  CONSTRAINT uq_deal_contact_association UNIQUE (deal_id, contact_id)
);

CREATE INDEX IF NOT EXISTS idx_deal_assoc_deal
  ON hubspot_deal_contact_association(deal_id);
CREATE INDEX IF NOT EXISTS idx_deal_assoc_contact
  ON hubspot_deal_contact_association(contact_id);
CREATE INDEX IF NOT EXISTS idx_deal_assoc_primary
  ON hubspot_deal_contact_association(deal_id, is_primary);

-- Sync watermark / coverage for the canonical deal ledger. Completeness is
-- explicit rather than inferred from "some rows exist": a failed sync must be
-- visible AS failed, never as a successful zero-row result.
CREATE TABLE IF NOT EXISTS hubspot_deal_sync_state (
  id                        SERIAL PRIMARY KEY,
  scope                     TEXT NOT NULL UNIQUE,   -- 'deals'
  bootstrap_status          TEXT NOT NULL DEFAULT 'not_started',
  bootstrap_started_at      TIMESTAMPTZ,
  bootstrap_completed_at    TIMESTAMPTZ,
  last_modified_watermark   TIMESTAMPTZ,
  last_incremental_at       TIMESTAMPTZ,
  last_status               TEXT,                   -- success|partial|failed
  last_error                TEXT,
  -- WHICH MODE produced last_status (PR-ADS-153E-A2). Without it, a bootstrap
  -- rerun's `success` could validate an incremental timestamp it never wrote:
  -- bootstrap completes at T0 → incremental FAILS at T1 → bootstrap reruns
  -- successfully at T2, preserving T0 and T1 but overwriting last_status. The
  -- audit then saw T1 > T0 and success, and passed — with no successful
  -- incremental after the bootstrap anywhere in that history.
  last_sync_mode            TEXT,                   -- bootstrap|incremental
  deals_seen                INTEGER DEFAULT 0,
  pages_fetched             INTEGER DEFAULT 0,
  association_failures      INTEGER DEFAULT 0,
  last_batch_id             INTEGER REFERENCES sync_batches(id) ON DELETE SET NULL,
  updated_at                TIMESTAMPTZ DEFAULT NOW()
);

-- PR-ADS-153E-A2 migration for databases created before `last_sync_mode`
-- existed. Idempotent and additive; NULL on existing rows, which FAILS CLOSED
-- in the audit until a real sync records the mode.
ALTER TABLE hubspot_deal_sync_state
  ADD COLUMN IF NOT EXISTS last_sync_mode TEXT;

-- PR-ADS-153F: per-chunk fetch ledger for canonical Google Ads GEO spend, the
-- exact counterpart of google_ads_spend_coverage. Before this table the geo sync
-- had no durable evidence at all: a range that was never fetched and a range
-- that was fetched and genuinely had no country-attributable spend were
-- indistinguishable, so staleness was invisible on every health surface and a
-- recovery run had to re-fetch history it had already proven.
--
-- It is a SEPARATE table rather than a `dataset` column on
-- google_ads_spend_coverage because that table's identity is
-- (customer_id, chunk_start, chunk_end): campaign coverage and geo coverage for
-- the same customer and the same range would collide on one row and silently
-- overwrite each other. Widening a production unique key is also a destructive
-- migration; adding a table is additive and idempotent.
--
-- `status` is verified | failed, and a `failed` write never demotes a chunk that
-- is already `verified` (the ON CONFLICT guard in db.writers.upsert_geo_coverage)
-- — the same rule the campaign-spend ledger uses.
CREATE TABLE IF NOT EXISTS google_ads_geo_coverage (
  id                    SERIAL PRIMARY KEY,
  customer_id           TEXT NOT NULL,
  chunk_start           DATE NOT NULL,
  chunk_end             DATE NOT NULL,
  status                TEXT NOT NULL,          -- verified | failed
  rows_written          INTEGER NOT NULL DEFAULT 0,
  cost_micros_total     BIGINT NOT NULL DEFAULT 0,
  country_count         INTEGER NOT NULL DEFAULT 0,
  -- Internal diagnostics only. Never rendered to an end user and never used to
  -- decide availability — a chunk is unusable because its status is not
  -- `verified`, not because of what this string says.
  error_message         TEXT,
  source_query_version  TEXT,
  sync_run_id           TEXT,
  fetched_at            TIMESTAMPTZ DEFAULT NOW(),
  updated_at            TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (customer_id, chunk_start, chunk_end)
);

CREATE INDEX IF NOT EXISTS idx_ga_geo_coverage_range
  ON google_ads_geo_coverage(chunk_start, chunk_end);

-- PR-ADS-153F: durable run/checkpoint state for the canonical geo sync, so a
-- resumed run knows where it stopped and every health surface can see when geo
-- last completed successfully. One row per (customer, scope) — `scope` is
-- 'geo_daily_spend' today and exists so a second geo dataset cannot silently
-- reuse this row.
--
-- last_successful_completed_at advances ONLY after the coverage ledger and the
-- spend rows for that run are committed. Publishing freshness before the write
-- lands is how a partial run starts looking healthy.
CREATE TABLE IF NOT EXISTS google_ads_geo_sync_state (
  id                            SERIAL PRIMARY KEY,
  customer_id                   TEXT NOT NULL,
  scope                         TEXT NOT NULL DEFAULT 'geo_daily_spend',
  last_status                   TEXT,           -- success|partial|failed|running
  last_started_at               TIMESTAMPTZ,
  last_finished_at              TIMESTAMPTZ,
  last_successful_completed_at  TIMESTAMPTZ,
  -- The resume checkpoint: the newest date proven covered by a verified chunk.
  checkpoint_date               DATE,
  requested_start               DATE,
  requested_end                 DATE,
  chunks_verified               INTEGER NOT NULL DEFAULT 0,
  chunks_failed                 INTEGER NOT NULL DEFAULT 0,
  chunks_skipped                INTEGER NOT NULL DEFAULT 0,
  rows_written                  INTEGER NOT NULL DEFAULT 0,
  last_error                    TEXT,
  last_run_id                   TEXT,
  -- PR-ADS-153F: the lease FENCING token. Expiry alone is not ownership: if
  -- worker A overruns the lease window and worker B legitimately reclaims it,
  -- A can still be running and would otherwise overwrite B's state on finish.
  -- Terminal writes are conditioned on this token, so a stale worker's write
  -- simply matches nothing.
  lease_token                   TEXT,
  -- PR-ADS-153F: the lease DEADLINE, renewed by a heartbeat while the owner is
  -- alive. `last_started_at + a fixed window` cannot express this: the
  -- historical backfill runs for many monthly chunks and would silently pass
  -- its own deadline mid-run, letting a second worker legitimately claim the
  -- lease while the first kept writing geo rows and coverage. A renewable
  -- deadline keeps crash recovery (it still lapses if nobody renews it) while
  -- letting a long, healthy run stay the owner.
  lease_expires_at              TIMESTAMPTZ,
  updated_at                    TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (customer_id, scope)
);

-- Existing databases: add the fencing token and the lease deadline if the table
-- predates them.
ALTER TABLE google_ads_geo_sync_state ADD COLUMN IF NOT EXISTS lease_token TEXT;
ALTER TABLE google_ads_geo_sync_state ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;

-- PR-ADS-153F additive integrity constraints. Each is a rule the writers
-- already enforce; stating it in the schema means a future writer, a migration
-- or a manual fix cannot quietly produce a row the readers would misinterpret.
-- Added NOT VALID-free because both tables are new in this PR and hold no rows
-- that could violate them.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_geo_coverage_status') THEN
    ALTER TABLE google_ads_geo_coverage
      ADD CONSTRAINT ck_geo_coverage_status
      CHECK (status IN ('verified', 'failed'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_geo_coverage_range') THEN
    ALTER TABLE google_ads_geo_coverage
      ADD CONSTRAINT ck_geo_coverage_range
      CHECK (chunk_start <= chunk_end);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_geo_sync_state_status') THEN
    ALTER TABLE google_ads_geo_sync_state
      ADD CONSTRAINT ck_geo_sync_state_status
      CHECK (last_status IS NULL
             OR last_status IN ('running', 'success', 'partial', 'failed'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_geo_sync_state_range') THEN
    ALTER TABLE google_ads_geo_sync_state
      ADD CONSTRAINT ck_geo_sync_state_range
      CHECK (requested_start IS NULL OR requested_end IS NULL
             OR requested_start <= requested_end);
  END IF;
END $$;

-- PR-ADS-154: fold the superseded `google_ads` source spelling onto the one
-- canonical `google_ads_api` key.
--
-- Both spellings named the same platform-evidence source. Writers now
-- canonicalize before stamping (services/dataset_keys.canonical_source), so no
-- new row can carry the old spelling — but production already holds rows under
-- it, including the successful historical geo bootstrap's batches. Leaving them
-- behind would orphan that history and the affected datasets would report
-- "never run" again, which is the same defect wearing the opposite mask.
--
-- This relabels BOOKKEEPING rows only. No evidence table is touched:
-- google_ads_geo_daily_spend, google_ads_geo_coverage and
-- google_ads_campaign_daily_spend are untouched, so the bootstrap's populated
-- data is preserved exactly.
--
-- Idempotent and collision-safe: `sync_state` is UNIQUE(source, dataset), so a
-- dataset that somehow has BOTH spellings is resolved by keeping the row with
-- the newer successful sync and dropping the staler duplicate, rather than
-- failing the migration or silently keeping the older answer.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM sync_state WHERE source = 'google_ads') THEN
    DELETE FROM sync_state stale
     USING sync_state keep
     WHERE stale.source = 'google_ads'
       AND keep.source  = 'google_ads_api'
       AND keep.dataset = stale.dataset
       AND COALESCE(keep.last_successful_sync_at, '-infinity'::timestamptz)
           >= COALESCE(stale.last_successful_sync_at, '-infinity'::timestamptz);

    DELETE FROM sync_state superseded
     USING sync_state fresher
     WHERE superseded.source = 'google_ads_api'
       AND fresher.source    = 'google_ads'
       AND fresher.dataset   = superseded.dataset
       AND COALESCE(fresher.last_successful_sync_at, '-infinity'::timestamptz)
           > COALESCE(superseded.last_successful_sync_at, '-infinity'::timestamptz);

    UPDATE sync_state SET source = 'google_ads_api' WHERE source = 'google_ads';
  END IF;

  -- sync_batches is an append-only history with no uniqueness constraint on
  -- (source, dataset), so it relabels unconditionally.
  UPDATE sync_batches SET source = 'google_ads_api' WHERE source = 'google_ads';
END $$;

-- PR-ADS-154A: widen `runs.run_type` on databases created before the column
-- was VARCHAR(64).
--
-- `CREATE TABLE IF NOT EXISTS` above only shapes NEW databases; production's
-- `runs` table already exists, so without this migration the deployed schema
-- keeps VARCHAR(20) and every incremental run keeps failing on INSERT. It runs
-- through the normal `init_db()` deployment path precisely so nobody has to
-- remember a manual production SQL command.
--
-- Guarded on the current length so a redeploy is a no-op rather than a
-- repeated DDL statement. Widening a varchar is a catalog-only change in
-- PostgreSQL — no table rewrite, no lock beyond a brief ACCESS EXCLUSIVE, and
-- every existing row keeps its value untouched. Shorter run types ('daily',
-- 'backfill', 'revenue_recovery') are unaffected: a wider domain still
-- contains them.
DO $$
DECLARE
  current_len INTEGER;
BEGIN
  -- Scoped to the ACTIVE schema. Unscoped, a database with more than one
  -- schema (or a non-default search_path) could match a different `runs` table
  -- and either skip the widening the real table needs or apply it blindly.
  -- `current_schema()` is the same schema the ALTER below resolves to, so the
  -- guard and the action can never disagree about which table they mean.
  SELECT character_maximum_length INTO current_len
    FROM information_schema.columns
   WHERE table_schema = current_schema()
     AND table_name = 'runs'
     AND column_name = 'run_type';

  IF current_len IS NOT NULL AND current_len < 64 THEN
    ALTER TABLE runs ALTER COLUMN run_type TYPE VARCHAR(64);
    RAISE NOTICE 'runs.run_type widened from VARCHAR(%) to VARCHAR(64)', current_len;
  END IF;
END $$;
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
