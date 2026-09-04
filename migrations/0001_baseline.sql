-- 0001_baseline
--
-- Establishes migration tracking. schema.sql remains the baseline for a fresh
-- database; every change made after it lives in a numbered file here.
--
-- Applying this to a database that already ran schema.sql is a no-op beyond
-- creating the ledger.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    checksum    TEXT
);

COMMENT ON TABLE schema_migrations IS
    'Applied database migrations. Maintained by scripts/migrate.py.';
