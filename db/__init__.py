"""
db/

PostgreSQL persistence layer for the Logistaas Ads Intelligence System.

Modules:
  connection — ThreadedConnectionPool; non-fatal if DATABASE_URL is absent.
  schema     — CREATE TABLE IF NOT EXISTS; call init_db() once on startup.
  writers    — write_run, write_campaigns, write_leads, write_waste_terms,
                write_deals; all wrapped in try/except — never fatal.
"""
