-- FOS Coverage VNext schema, MariaDB 5.5-compatible.
-- This file is for a separate Candidate target database. The migration
-- runner checks information_schema before executing it; no production table
-- is altered by this script.

CREATE TABLE IF NOT EXISTS coverage_schema_meta (
    schema_key VARCHAR(64) NOT NULL,
    schema_version INT NOT NULL,
    applied_at DATETIME(6) NOT NULL,
    release_sha CHAR(40) NOT NULL DEFAULT '',
    migration_id VARCHAR(128) NOT NULL DEFAULT '',
    PRIMARY KEY (schema_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coverage_schema_migrations (
    migration_id VARCHAR(128) NOT NULL,
    schema_key VARCHAR(64) NOT NULL,
    from_version INT NOT NULL DEFAULT 0,
    to_version INT NOT NULL,
    ddl_sha256 CHAR(64) NOT NULL,
    state VARCHAR(16) NOT NULL,
    started_at DATETIME NOT NULL,
    finished_at DATETIME NULL,
    release_sha CHAR(40) NOT NULL DEFAULT '',
    error_class VARCHAR(128) NOT NULL DEFAULT '',
    target_database VARCHAR(128) NOT NULL DEFAULT '',
    target_runtime_fingerprint VARCHAR(255) NOT NULL DEFAULT '',
    target_table_inventory_hash CHAR(64) NOT NULL DEFAULT '',
    target_emptiness_result VARCHAR(64) NOT NULL DEFAULT '',
    target_preflight_at DATETIME NULL,
    PRIMARY KEY (migration_id),
    KEY idx_schema_migrations_key (schema_key, started_at)
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
    predecessor_scan_id BIGINT NULL,
    algorithm_version VARCHAR(64) NOT NULL DEFAULT '',
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
    repository_id BIGINT NULL,
    commit_sha CHAR(40) NULL,
    identity_verified TINYINT NOT NULL DEFAULT 0,
    identity_provenance VARCHAR(128) NOT NULL DEFAULT '',
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
    report_mode VARCHAR(32) NOT NULL DEFAULT 'LEGACY_STATIC',
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
    ordinary_pending_total INT NOT NULL DEFAULT 0,
    inherited_pending_total INT NOT NULL DEFAULT 0,
    manual_draft_pending_total INT NOT NULL DEFAULT 0,
    data_version BIGINT NOT NULL DEFAULT 0,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (scan_id, file_id),
    KEY idx_vnext_file_state_pending (scan_id, pending_total, file_id),
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
    handler_version VARCHAR(64) NOT NULL DEFAULT '',
    legacy_raw_percent DECIMAL(12,3) NULL,
    legacy_percent_unit VARCHAR(32) NOT NULL DEFAULT '',
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
    KEY idx_vnext_jobs_active_identity (
        project_id, scan_id, kind, data_version, state, created_at, job_id
    ),
    KEY idx_vnext_jobs_recovery (state, heartbeat_at, created_at, job_id),
    KEY idx_vnext_jobs_created_cursor (created_at, job_id),
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
    incremental_key_hash CHAR(64) NOT NULL,
    old_commit_sha CHAR(40) NOT NULL DEFAULT '',
    new_commit_sha CHAR(40) NOT NULL DEFAULT '',
    payload LONGTEXT NOT NULL,
    generated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_incremental_key_hash (incremental_key_hash),
    KEY idx_incremental_scan_repo (scan_id, report_id(64), repository_name(123)),
    CONSTRAINT fk_incremental_result_scan FOREIGN KEY (scan_id)
        REFERENCES coverage_scans(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Gate A: source field provenance is persisted in the target, independent of
-- the external anomaly JSON.  The table is append-only by migration identity.
CREATE TABLE IF NOT EXISTS coverage_legacy_provenance (
    id BIGINT NOT NULL AUTO_INCREMENT,
    migration_id VARCHAR(128) NOT NULL,
    target_entity_type VARCHAR(32) NOT NULL,
    target_entity_id BIGINT NOT NULL,
    source_table VARCHAR(64) NOT NULL,
    source_identity VARCHAR(512) NOT NULL,
    provenance_key_hash CHAR(64) NOT NULL,
    legacy_created_at DATETIME(6) NULL,
    legacy_updated_at DATETIME(6) NULL,
    legacy_raw_status VARCHAR(64) NULL,
    legacy_raw_is_draft TINYINT NULL,
    raw_payload_sha256 CHAR(64) NOT NULL,
    raw_payload LONGTEXT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_legacy_provenance_hash (provenance_key_hash),
    KEY idx_legacy_provenance_source (source_table(30), source_identity(159))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Gate B: stable logical repositories and their physical Git resources.
CREATE TABLE IF NOT EXISTS coverage_repositories (
    id BIGINT NOT NULL AUTO_INCREMENT,
    project_id BIGINT NOT NULL,
    repository_name VARCHAR(128) NOT NULL,
    canonical_remote VARCHAR(1024) NULL,
    last_observed_physical_path VARCHAR(1024) NOT NULL DEFAULT '',
    physical_resource_id BIGINT NULL,
    lifecycle_state VARCHAR(16) NOT NULL DEFAULT 'ACTIVE',
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_coverage_repository_name (project_id, repository_name),
    KEY idx_coverage_repository_resource (physical_resource_id),
    CONSTRAINT fk_coverage_repositories_project FOREIGN KEY (project_id)
        REFERENCES coverage_projects(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coverage_repository_aliases (
    id BIGINT NOT NULL AUTO_INCREMENT,
    project_id BIGINT NOT NULL,
    repository_id BIGINT NOT NULL,
    alias_name VARCHAR(128) NOT NULL,
    created_at DATETIME NOT NULL,
    retired_at DATETIME NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_coverage_repository_alias (project_id, alias_name),
    CONSTRAINT fk_coverage_repository_alias_project FOREIGN KEY (project_id)
        REFERENCES coverage_projects(id),
    CONSTRAINT fk_coverage_repository_alias_repository FOREIGN KEY (repository_id)
        REFERENCES coverage_repositories(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coverage_repository_resources (
    id BIGINT NOT NULL AUTO_INCREMENT,
    resource_key CHAR(64) NOT NULL,
    resolved_git_common_dir VARCHAR(1024) NOT NULL,
    resolved_worktree_root VARCHAR(1024) NOT NULL,
    fs_device BIGINT NULL,
    fs_inode BIGINT NULL,
    next_fencing_token BIGINT NOT NULL DEFAULT 0,
    observed_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_coverage_resource_key (resource_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Gate B: content, human range and current physical-line relation are three
-- separate authorities.  No review-state column exists on the content row.
CREATE TABLE IF NOT EXISTS coverage_analysis_records (
    id BIGINT NOT NULL AUTO_INCREMENT,
    conclusion_status VARCHAR(64) NOT NULL DEFAULT '',
    coverage_method TEXT NULL,
    uncovered_reason TEXT NULL,
    comment TEXT NULL,
    content_revision BIGINT NOT NULL DEFAULT 1,
    content_hash CHAR(64) NOT NULL,
    content_origin VARCHAR(32) NOT NULL DEFAULT 'MANUAL',
    legacy_source_analysis_id BIGINT NULL,
    legacy_source_created_at DATETIME(6) NULL,
    legacy_source_updated_at DATETIME(6) NULL,
    legacy_raw_status VARCHAR(64) NULL,
    legacy_raw_is_draft TINYINT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    KEY idx_analysis_record_hash (content_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coverage_analysis_blocks (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scan_id BIGINT NOT NULL,
    repository_id BIGINT NULL,
    file_id BIGINT NOT NULL,
    start_line INT NOT NULL,
    end_line INT NOT NULL,
    origin VARCHAR(32) NOT NULL DEFAULT 'MANUAL',
    block_identity_verified TINYINT NOT NULL DEFAULT 0,
    originating_record_id BIGINT NULL,
    initial_content_hash CHAR(64) NULL,
    created_by VARCHAR(255) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    KEY idx_analysis_blocks_scan_file (scan_id, file_id, start_line, end_line),
    CONSTRAINT fk_analysis_blocks_scan FOREIGN KEY (scan_id)
        REFERENCES coverage_scans(id),
    CONSTRAINT fk_analysis_blocks_file FOREIGN KEY (file_id)
        REFERENCES coverage_files(id),
    CONSTRAINT fk_analysis_blocks_record FOREIGN KEY (originating_record_id)
        REFERENCES coverage_analysis_records(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coverage_inheritance_groups (
    id BIGINT NOT NULL AUTO_INCREMENT,
    decision_run_id CHAR(64) NOT NULL,
    candidate_scan_id BIGINT NOT NULL,
    source_scan_id BIGINT NOT NULL,
    source_analysis_block_id BIGINT NOT NULL,
    repository_id BIGINT NOT NULL,
    candidate_file_id BIGINT NOT NULL,
    mapping_fingerprint CHAR(64) NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_inheritance_group (
        decision_run_id, source_analysis_block_id, candidate_file_id,
        mapping_fingerprint
    ),
    CONSTRAINT fk_inheritance_group_candidate_scan FOREIGN KEY (candidate_scan_id)
        REFERENCES coverage_scans(id),
    CONSTRAINT fk_inheritance_group_source_scan FOREIGN KEY (source_scan_id)
        REFERENCES coverage_scans(id),
    CONSTRAINT fk_inheritance_group_block FOREIGN KEY (source_analysis_block_id)
        REFERENCES coverage_analysis_blocks(id),
    CONSTRAINT fk_inheritance_group_file FOREIGN KEY (candidate_file_id)
        REFERENCES coverage_files(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coverage_analysis_line_links (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scan_id BIGINT NOT NULL,
    line_id BIGINT NOT NULL,
    analysis_record_id BIGINT NOT NULL,
    analysis_block_id BIGINT NULL,
    review_state VARCHAR(32) NOT NULL,
    relation_origin VARCHAR(32) NOT NULL,
    inheritance_group_id BIGINT NULL,
    is_active TINYINT NOT NULL DEFAULT 1,
    reviewed_by VARCHAR(255) NOT NULL DEFAULT '',
    reviewed_at DATETIME NULL,
    source_scan_id BIGINT NULL,
    source_line_id BIGINT NULL,
    source_relation_id BIGINT NULL,
    relation_revision BIGINT NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_analysis_line_link_line (scan_id, line_id),
    KEY idx_analysis_line_link_record (analysis_record_id),
    KEY idx_analysis_line_link_state (scan_id, review_state, is_active),
    KEY idx_vnext_links_scan_active_line (scan_id, is_active, line_id),
    CONSTRAINT fk_analysis_line_link_scan FOREIGN KEY (scan_id)
        REFERENCES coverage_scans(id),
    CONSTRAINT fk_analysis_line_link_line FOREIGN KEY (line_id)
        REFERENCES coverage_lines(id),
    CONSTRAINT fk_analysis_line_link_record FOREIGN KEY (analysis_record_id)
        REFERENCES coverage_analysis_records(id),
    CONSTRAINT fk_analysis_line_link_block FOREIGN KEY (analysis_block_id)
        REFERENCES coverage_analysis_blocks(id),
    CONSTRAINT fk_analysis_line_link_group FOREIGN KEY (inheritance_group_id)
        REFERENCES coverage_inheritance_groups(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coverage_inheritance_decisions (
    id BIGINT NOT NULL AUTO_INCREMENT,
    decision_run_id CHAR(64) NOT NULL,
    candidate_scan_id BIGINT NOT NULL,
    candidate_line_id BIGINT NOT NULL,
    source_scan_id BIGINT NULL,
    source_line_id BIGINT NULL,
    source_relation_id BIGINT NULL,
    decision VARCHAR(32) NOT NULL,
    reason_code VARCHAR(64) NOT NULL,
    algorithm_version VARCHAR(64) NOT NULL,
    old_commit_sha CHAR(40) NULL,
    new_commit_sha CHAR(40) NULL,
    line_mapping_fingerprint CHAR(64) NOT NULL DEFAULT '',
    function_identity_fingerprint CHAR(64) NOT NULL DEFAULT '',
    control_context_fingerprint CHAR(64) NOT NULL DEFAULT '',
    preprocessor_context_fingerprint CHAR(64) NOT NULL DEFAULT '',
    dependency_fingerprint CHAR(64) NOT NULL DEFAULT '',
    evaluated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_inheritance_decision (decision_run_id, candidate_line_id),
    KEY idx_inheritance_decisions_scan (candidate_scan_id, decision),
    CONSTRAINT fk_inheritance_decision_scan FOREIGN KEY (candidate_scan_id)
        REFERENCES coverage_scans(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coverage_inheritance_rejections (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scan_id BIGINT NOT NULL,
    line_id BIGINT NOT NULL,
    rejected_relation_id BIGINT NOT NULL,
    rejected_relation_revision BIGINT NOT NULL,
    rejected_analysis_record_id BIGINT NOT NULL,
    rejected_source_scan_id BIGINT NULL,
    rejected_source_line_id BIGINT NULL,
    rejected_source_relation_id BIGINT NULL,
    rejection_revision BIGINT NOT NULL DEFAULT 1,
    is_active TINYINT NOT NULL DEFAULT 1,
    terminal_reason VARCHAR(32) NULL,
    rejected_by VARCHAR(255) NOT NULL,
    rejected_at DATETIME NOT NULL,
    resolved_at DATETIME NULL,
    PRIMARY KEY (id),
    KEY idx_inheritance_rejection_active (scan_id, line_id, is_active),
    CONSTRAINT fk_inheritance_rejection_scan FOREIGN KEY (scan_id)
        REFERENCES coverage_scans(id),
    CONSTRAINT fk_inheritance_rejection_line FOREIGN KEY (line_id)
        REFERENCES coverage_lines(id),
    CONSTRAINT fk_inheritance_rejection_relation FOREIGN KEY (rejected_relation_id)
        REFERENCES coverage_analysis_line_links(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Gate C: durable import state.  Every stateful write checks job/lock fencing.
CREATE TABLE IF NOT EXISTS coverage_repository_resource_locks (
    physical_resource_id BIGINT NOT NULL,
    job_id VARCHAR(64) NOT NULL,
    owner_token VARCHAR(128) NOT NULL,
    fencing_token BIGINT NOT NULL,
    heartbeat_at DATETIME NOT NULL,
    acquired_at DATETIME NOT NULL,
    expires_at DATETIME NULL,
    PRIMARY KEY (physical_resource_id),
    UNIQUE KEY uq_resource_lock_owner (job_id, physical_resource_id),
    CONSTRAINT fk_resource_lock_resource FOREIGN KEY (physical_resource_id)
        REFERENCES coverage_repository_resources(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coverage_import_artifacts (
    artifact_id VARCHAR(128) NOT NULL,
    job_id VARCHAR(64) NOT NULL,
    kind VARCHAR(32) NOT NULL,
    staged_path VARCHAR(1024) NOT NULL,
    sha256 CHAR(64) NOT NULL,
    size_bytes BIGINT NOT NULL DEFAULT 0,
    immutable TINYINT NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (artifact_id),
    UNIQUE KEY uq_import_artifact_job_kind (job_id, kind),
    KEY idx_import_artifact_sha (sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coverage_import_checkpoints (
    job_id VARCHAR(64) NOT NULL,
    scan_id BIGINT NULL,
    phase VARCHAR(64) NOT NULL,
    phase_version INT NOT NULL DEFAULT 1,
    checkpoint_seq BIGINT NOT NULL DEFAULT 0,
    payload LONGTEXT NOT NULL,
    input_sha256 CHAR(64) NOT NULL DEFAULT '',
    fencing_token BIGINT NOT NULL DEFAULT 0,
    expected_current_scan_id BIGINT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (job_id),
    CONSTRAINT fk_import_checkpoint_scan FOREIGN KEY (scan_id)
        REFERENCES coverage_scans(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coverage_import_failures (
    id BIGINT NOT NULL AUTO_INCREMENT,
    job_id VARCHAR(64) NOT NULL,
    scan_id BIGINT NULL,
    phase VARCHAR(64) NOT NULL,
    error_class VARCHAR(64) NOT NULL,
    error_fingerprint CHAR(64) NOT NULL,
    failure_key_hash CHAR(64) NOT NULL,
    message_redacted TEXT NULL,
    fencing_token BIGINT NULL,
    occurred_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_import_failure_hash (failure_key_hash),
    KEY idx_import_failure_lookup (job_id(61), phase(64), error_fingerprint(64)),
    CONSTRAINT fk_import_failure_scan FOREIGN KEY (scan_id)
        REFERENCES coverage_scans(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coverage_migration_checkpoints (
    migration_id VARCHAR(128) NOT NULL,
    checkpoint_key TEXT NOT NULL,
    checkpoint_key_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    phase VARCHAR(64) NOT NULL,
    source_cursor VARCHAR(512) NOT NULL DEFAULT '',
    semantic_fragment_hash CHAR(64) NOT NULL DEFAULT '',
    target_counts LONGTEXT NOT NULL,
    migration_version INT NOT NULL DEFAULT 1,
    state VARCHAR(32) NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (checkpoint_key_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
