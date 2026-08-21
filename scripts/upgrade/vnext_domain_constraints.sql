-- Gate B domain constraint migration, MariaDB 5.5-compatible.
--
-- This file is executed by domain_migration.py one statement at a time after
-- the core tables exist.  It is intentionally separate from
-- vnext_schema.sql: MariaDB 5.5 has no reliable ADD CONSTRAINT IF NOT EXISTS,
-- so the migration ledger and information_schema checks own idempotency.
-- Every relationship uses the default historical-fact policy explicitly:
-- RESTRICT.  No analysis, inheritance, job, or resource-lock chain may be
-- silently removed by a parent delete.
-- Import artifact/checkpoint/failure and resource-lock job_id intentionally
-- remain application-validated rather than FK-bound: the durable coordinator
-- acquires the physical lock and stages the immutable artifact before it can
-- persist the background-job row.  Adding that FK would reject the required
-- crash-safe creation order.

ALTER TABLE coverage_repositories
    ADD CONSTRAINT fk_coverage_repositories_resource
    FOREIGN KEY (physical_resource_id)
    REFERENCES coverage_repository_resources(id)
    ON DELETE RESTRICT;

ALTER TABLE coverage_scan_repositories
    ADD KEY idx_scan_repositories_repository (repository_id),
    ADD CONSTRAINT fk_scan_repositories_repository
    FOREIGN KEY (repository_id)
    REFERENCES coverage_repositories(id)
    ON DELETE RESTRICT;

ALTER TABLE coverage_analysis_blocks
    ADD KEY idx_analysis_blocks_repository (repository_id),
    ADD CONSTRAINT fk_analysis_blocks_repository
    FOREIGN KEY (repository_id)
    REFERENCES coverage_repositories(id)
    ON DELETE RESTRICT;

ALTER TABLE coverage_inheritance_groups
    ADD KEY idx_inheritance_groups_repository (repository_id),
    ADD CONSTRAINT fk_inheritance_groups_repository
    FOREIGN KEY (repository_id)
    REFERENCES coverage_repositories(id)
    ON DELETE RESTRICT;

ALTER TABLE coverage_analysis_line_links
    ADD KEY idx_analysis_line_link_source_scan (source_scan_id),
    ADD KEY idx_analysis_line_link_source_line (source_line_id),
    ADD KEY idx_analysis_line_link_source_relation (source_relation_id),
    ADD CONSTRAINT fk_analysis_line_link_source_scan
    FOREIGN KEY (source_scan_id)
    REFERENCES coverage_scans(id)
    ON DELETE RESTRICT,
    ADD CONSTRAINT fk_analysis_line_link_source_line
    FOREIGN KEY (source_line_id)
    REFERENCES coverage_lines(id)
    ON DELETE RESTRICT,
    ADD CONSTRAINT fk_analysis_line_link_source_relation
    FOREIGN KEY (source_relation_id)
    REFERENCES coverage_analysis_line_links(id)
    ON DELETE RESTRICT;

ALTER TABLE coverage_inheritance_decisions
    ADD KEY idx_inheritance_decisions_candidate_line (candidate_line_id),
    ADD KEY idx_inheritance_decisions_source_scan (source_scan_id),
    ADD KEY idx_inheritance_decisions_source_line (source_line_id),
    ADD KEY idx_inheritance_decisions_source_relation (source_relation_id),
    ADD CONSTRAINT fk_inheritance_decision_candidate_line
    FOREIGN KEY (candidate_line_id)
    REFERENCES coverage_lines(id)
    ON DELETE RESTRICT,
    ADD CONSTRAINT fk_inheritance_decision_source_scan
    FOREIGN KEY (source_scan_id)
    REFERENCES coverage_scans(id)
    ON DELETE RESTRICT,
    ADD CONSTRAINT fk_inheritance_decision_source_line
    FOREIGN KEY (source_line_id)
    REFERENCES coverage_lines(id)
    ON DELETE RESTRICT,
    ADD CONSTRAINT fk_inheritance_decision_source_relation
    FOREIGN KEY (source_relation_id)
    REFERENCES coverage_analysis_line_links(id)
    ON DELETE RESTRICT;

ALTER TABLE coverage_inheritance_rejections
    ADD KEY idx_inheritance_rejection_record (rejected_analysis_record_id),
    ADD KEY idx_inheritance_rejection_source_scan (rejected_source_scan_id),
    ADD KEY idx_inheritance_rejection_source_line (rejected_source_line_id),
    ADD KEY idx_inheritance_rejection_source_relation (rejected_source_relation_id),
    ADD CONSTRAINT fk_inheritance_rejection_record
    FOREIGN KEY (rejected_analysis_record_id)
    REFERENCES coverage_analysis_records(id)
    ON DELETE RESTRICT,
    ADD CONSTRAINT fk_inheritance_rejection_source_scan
    FOREIGN KEY (rejected_source_scan_id)
    REFERENCES coverage_scans(id)
    ON DELETE RESTRICT,
    ADD CONSTRAINT fk_inheritance_rejection_source_line
    FOREIGN KEY (rejected_source_line_id)
    REFERENCES coverage_lines(id)
    ON DELETE RESTRICT,
    ADD CONSTRAINT fk_inheritance_rejection_source_relation
    FOREIGN KEY (rejected_source_relation_id)
    REFERENCES coverage_analysis_line_links(id)
    ON DELETE RESTRICT;

ALTER TABLE coverage_import_checkpoints
    ADD CONSTRAINT fk_import_checkpoint_expected_scan
    FOREIGN KEY (expected_current_scan_id)
    REFERENCES coverage_scans(id)
    ON DELETE RESTRICT;
