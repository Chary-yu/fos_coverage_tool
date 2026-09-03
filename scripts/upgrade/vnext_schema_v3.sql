-- Existing-VNext runtime migration v3, MariaDB 5.5 compatible.
--
-- This file is intentionally declarative.  The migration runner executes each
-- ADD only after an information_schema/PRAGMA existence and shape check.  Do
-- not add IF NOT EXISTS here: MariaDB 5.5 does not provide a reliable form of
-- that syntax for columns and indexes.
--
-- v3 contains no table rewrites and no authoritative-fact mutation.  The
-- FileState projection is rebuilt separately by existing_vnext_upgrade.py.

ALTER TABLE coverage_reports
    ADD COLUMN report_mode VARCHAR(32) NOT NULL DEFAULT 'LEGACY_STATIC';
