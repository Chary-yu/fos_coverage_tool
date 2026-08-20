-- FOS Coverage Tool Schema Migration v2 (Additive Migration - Item 7 & 8)
-- Safe, non-destructive additive table creation and index additions

CREATE TABLE IF NOT EXISTS coverage_file_state (
    project_name VARCHAR(128) NOT NULL,
    file_path_hash VARCHAR(64) NOT NULL,
    file_path VARCHAR(512) NOT NULL DEFAULT '',
    total_uncovered INT NOT NULL DEFAULT 0,
    filled_total INT NOT NULL DEFAULT 0,
    draft_total INT NOT NULL DEFAULT 0,
    confirmed_total INT NOT NULL DEFAULT 0,
    coverable_total INT NOT NULL DEFAULT 0,
    uncoverable_total INT NOT NULL DEFAULT 0,
    redundant_total INT NOT NULL DEFAULT 0,
    data_version INT NOT NULL DEFAULT 1,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (project_name, file_path_hash),
    KEY idx_proj_state (project_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Derived-state readiness proof; never replaces authoritative data_version.
-- The legacy table column is added by run_upgrade.py only after its
-- information_schema existence check. MariaDB 5.5 does not support the
-- ADD COLUMN IF NOT EXISTS syntax reliably.
