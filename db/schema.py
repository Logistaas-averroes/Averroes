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
CREATE INDEX IF NOT EXISTS idx_leads_source_type ON leads(source_type);
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
