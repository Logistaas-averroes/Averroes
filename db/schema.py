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

-- PR-ADS-025C: source type tracking + index (idempotent migration for existing DBs)
-- New installs: source_type is already in the CREATE TABLE above; ALTER is a no-op.
-- Existing DBs: ALTER TABLE adds the column; existing rows will have source_type NULL
--   until the next weekly run populates them — this is expected and handled by frontend.
ALTER TABLE leads ADD COLUMN IF NOT EXISTS source_type VARCHAR(30);
CREATE INDEX IF NOT EXISTS idx_leads_source_type    ON leads(source_type);
-- PR-ADS-025E-FIX: index on leads(campaign_name) to prevent full table scans on backfill UPDATEs
CREATE INDEX IF NOT EXISTS idx_leads_campaign_name  ON leads(campaign_name);

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
