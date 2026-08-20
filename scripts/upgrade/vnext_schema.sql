-- FOS Coverage VNext schema, MariaDB 5.5-compatible.
-- This file is for a separate Candidate target database. The migration
-- runner checks information_schema before executing it; no production table
-- is altered by this script.

CREATE TABLE IF NOT EXISTS coverage_schema_meta (
    schema_key VARCHAR(64) NOT NULL,
    schema_version INT NOT NULL,
    applied_at DATETIME NOT NULL,
    release_sha CHAR(40) NOT NULL DEFAULT '',
    PRIMARY KEY (schema_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coverage_projects (
    id BIGINT NOT NULL AUTO_INCREMENT,
    project_name VARCHAR(128) NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_coverage_projects_name (project_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coverage_scans (
    id BIGINT NOT NULL AUTO_INCREMENT,
    project_id BIGINT NOT NULL,
    scan_key CHAR(64) NOT NULL,
    scan_type VARCHAR(32) NOT NULL,
    review_scope VARCHAR(32) NOT NULL,
    info_file_name VARCHAR(255) NOT NULL DEFAULT '',
    info_sha256 CHAR(64) NOT NULL DEFAULT '',
    imported_at DATETIME NOT NULL,
    status VARCHAR(32) NOT NULL,
    legacy_migrated TINYINT NOT NULL DEFAULT 0,
    metadata_version INT NOT NULL DEFAULT 1,
    PRIMARY KEY (id),
    UNIQUE KEY uq_coverage_scans_key (scan_key),
    KEY idx_coverage_scans_project (project_id),
    CONSTRAINT fk_coverage_scans_project FOREIGN KEY (project_id)
        REFERENCES coverage_projects(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coverage_scan_repositories (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scan_id BIGINT NOT NULL,
    repository_name VARCHAR(128) NOT NULL,
    repository_path VARCHAR(512) NOT NULL DEFAULT '',
    branch_name VARCHAR(255) NOT NULL DEFAULT '',
    old_commit_sha CHAR(40) NULL,
    new_commit_sha CHAR(40) NULL,
    verified TINYINT NOT NULL DEFAULT 0,
    captured_at DATETIME NOT NULL,
    provenance VARCHAR(128) NOT NULL DEFAULT '',
    PRIMARY KEY (id),
    UNIQUE KEY uq_scan_repository (scan_id, repository_name),
    CONSTRAINT fk_scan_repositories_scan FOREIGN KEY (scan_id)
        REFERENCES coverage_scans(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coverage_reports (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scan_id BIGINT NOT NULL,
    report_id VARCHAR(64) NOT NULL,
    report_root VARCHAR(1024) NOT NULL DEFAULT '',
    source_signature VARCHAR(128) NOT NULL DEFAULT '',
    sidecar_schema INT NOT NULL DEFAULT 0,
    asset_identity VARCHAR(128) NOT NULL DEFAULT '',
    generated_at DATETIME NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_coverage_reports_report_id (report_id),
    KEY idx_coverage_reports_scan (scan_id),
    CONSTRAINT fk_coverage_reports_scan FOREIGN KEY (scan_id)
        REFERENCES coverage_scans(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coverage_files (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scan_id BIGINT NOT NULL,
    repository_name VARCHAR(128) NOT NULL DEFAULT '',
    file_path_hash CHAR(32) NOT NULL,
    file_path VARCHAR(512) NOT NULL,
    source_file_name VARCHAR(255) NOT NULL DEFAULT '',
    PRIMARY KEY (id),
    UNIQUE KEY uq_coverage_file_identity (scan_id, repository_name, file_path_hash),
    KEY idx_coverage_files_scan (scan_id),
    CONSTRAINT fk_coverage_files_scan FOREIGN KEY (scan_id)
        REFERENCES coverage_scans(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coverage_lines (
    id BIGINT NOT NULL AUTO_INCREMENT,
    file_id BIGINT NOT NULL,
    line_number INT NOT NULL,
    line_text TEXT,
    coverage_state VARCHAR(32) NOT NULL DEFAULT 'unknown',
    block_start_line INT NOT NULL,
    block_end_line INT NOT NULL,
    block_type VARCHAR(64) NOT NULL DEFAULT 'single',
    function_name VARCHAR(512) NOT NULL DEFAULT '',
    function_hash VARCHAR(64) NOT NULL DEFAULT '',
    code_line_hash VARCHAR(64) NOT NULL DEFAULT '',
    code_occurrence INT NOT NULL DEFAULT 1,
    suggested_reviewer VARCHAR(255) NOT NULL DEFAULT '',
    PRIMARY KEY (id),
    UNIQUE KEY uq_coverage_line_identity (file_id, line_number),
    KEY idx_coverage_lines_file (file_id),
    KEY idx_coverage_lines_function (file_id, function_hash, code_line_hash, code_occurrence),
    CONSTRAINT fk_coverage_lines_file FOREIGN KEY (file_id)
        REFERENCES coverage_files(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coverage_analyses (
    id BIGINT NOT NULL AUTO_INCREMENT,
    line_id BIGINT NOT NULL,
    status VARCHAR(64) NOT NULL DEFAULT '',
    is_draft TINYINT NOT NULL DEFAULT 0,
    reviewer VARCHAR(255) NOT NULL DEFAULT '',
    coverage_method TEXT,
    uncovered_reason TEXT,
    comment TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_coverage_analysis_line (line_id),
    CONSTRAINT fk_coverage_analyses_line FOREIGN KEY (line_id)
        REFERENCES coverage_lines(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coverage_project_state (
    project_id BIGINT NOT NULL,
    current_scan_id BIGINT NULL,
    data_version BIGINT NOT NULL DEFAULT 0,
    file_state_version BIGINT NOT NULL DEFAULT 0,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (project_id),
    CONSTRAINT fk_project_state_project FOREIGN KEY (project_id)
        REFERENCES coverage_projects(id),
    CONSTRAINT fk_project_state_scan FOREIGN KEY (current_scan_id)
        REFERENCES coverage_scans(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coverage_file_state (
    scan_id BIGINT NOT NULL,
    file_id BIGINT NOT NULL,
    total_lines INT NOT NULL DEFAULT 0,
    total_uncovered INT NOT NULL DEFAULT 0,
    filled_total INT NOT NULL DEFAULT 0,
    draft_total INT NOT NULL DEFAULT 0,
    confirmed_total INT NOT NULL DEFAULT 0,
    pending_total INT NOT NULL DEFAULT 0,
    data_version BIGINT NOT NULL DEFAULT 0,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (scan_id, file_id),
    CONSTRAINT fk_file_state_scan FOREIGN KEY (scan_id)
        REFERENCES coverage_scans(id),
    CONSTRAINT fk_file_state_file FOREIGN KEY (file_id)
        REFERENCES coverage_files(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coverage_background_jobs (
    job_id VARCHAR(64) NOT NULL,
    project_id BIGINT NULL,
    scan_id BIGINT NULL,
    kind VARCHAR(64) NOT NULL,
    state VARCHAR(32) NOT NULL,
    progress DECIMAL(7,4) NOT NULL DEFAULT 0,
    input_payload LONGTEXT NOT NULL,
    result_path VARCHAR(1024) NOT NULL DEFAULT '',
    error_message TEXT,
    data_version BIGINT NULL,
    heartbeat_at DATETIME NULL,
    lease_owner VARCHAR(128) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL,
    started_at DATETIME NULL,
    finished_at DATETIME NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (job_id),
    KEY idx_vnext_jobs_project (project_id),
    KEY idx_vnext_jobs_scan (scan_id),
    KEY idx_vnext_jobs_state (state),
    CONSTRAINT fk_vnext_jobs_project FOREIGN KEY (project_id)
        REFERENCES coverage_projects(id),
    CONSTRAINT fk_vnext_jobs_scan FOREIGN KEY (scan_id)
        REFERENCES coverage_scans(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coverage_incremental_results (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scan_id BIGINT NOT NULL,
    report_id VARCHAR(64) NOT NULL DEFAULT '',
    repository_name VARCHAR(128) NOT NULL,
    old_commit_sha CHAR(40) NOT NULL DEFAULT '',
    new_commit_sha CHAR(40) NOT NULL DEFAULT '',
    payload LONGTEXT NOT NULL,
    generated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_incremental_scan_repo (scan_id, report_id, repository_name),
    CONSTRAINT fk_incremental_result_scan FOREIGN KEY (scan_id)
        REFERENCES coverage_scans(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
