-- Legacy compatibility fixture.  The fixture intentionally includes the
-- columns found in production inventories as well as nullable historical
-- metadata.  It is valid on MariaDB 5.5 and SQLite (the test builder adapts
-- AUTO_INCREMENT syntax when necessary).
CREATE TABLE coverage_analysis (
    id BIGINT NOT NULL AUTO_INCREMENT,
    project_name VARCHAR(255) NOT NULL,
    file_path TEXT NOT NULL,
    file_path_hash VARCHAR(128) NOT NULL,
    source_file_name VARCHAR(255) NOT NULL DEFAULT '',
    line_number INT NOT NULL,
    reviewer VARCHAR(255) NULL,
    status VARCHAR(64) NULL,
    is_draft TINYINT NULL,
    coverage_method TEXT NULL,
    uncovered_reason TEXT NULL,
    comment TEXT NULL,
    remark TEXT NULL,
    created_at DATETIME NULL,
    updated_at DATETIME NULL,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE coverage_line_index (
    id BIGINT NOT NULL AUTO_INCREMENT,
    project_name VARCHAR(255) NOT NULL,
    file_path TEXT NOT NULL,
    file_path_hash VARCHAR(128) NOT NULL,
    source_file_name VARCHAR(255) NOT NULL DEFAULT '',
    line_number INT NOT NULL,
    line_text TEXT NULL,
    block_start_line INT NULL,
    block_end_line INT NULL,
    block_type VARCHAR(64) NULL,
    function_name VARCHAR(512) NULL,
    function_hash VARCHAR(128) NULL,
    code_line_hash VARCHAR(128) NULL,
    code_occurrence INT NULL,
    created_at DATETIME NULL,
    updated_at DATETIME NULL,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE coverage_project_state (
    project_name VARCHAR(255) NOT NULL,
    data_version BIGINT NULL,
    file_state_version BIGINT NULL,
    current_scan_key VARCHAR(128) NULL,
    updated_at DATETIME NULL,
    PRIMARY KEY (project_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE coverage_background_jobs (
    job_id VARCHAR(128) NOT NULL,
    project_name VARCHAR(255) NULL,
    kind VARCHAR(128) NULL,
    state VARCHAR(64) NULL,
    percent DECIMAL(7,3) NULL,
    progress_unit VARCHAR(32) NULL,
    stage VARCHAR(255) NULL,
    message TEXT NULL,
    input_payload LONGTEXT NULL,
    result_path TEXT NULL,
    filename VARCHAR(255) NULL,
    row_count BIGINT NULL,
    data_version BIGINT NULL,
    heartbeat_at DATETIME NULL,
    finished_at DATETIME NULL,
    error_message TEXT NULL,
    created_at DATETIME NULL,
    started_at DATETIME NULL,
    updated_at DATETIME NULL,
    PRIMARY KEY (job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
