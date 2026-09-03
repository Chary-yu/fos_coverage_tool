import os
import sqlite3
import tempfile
import unittest

from scripts.upgrade.database_generation import LEGACY, UNKNOWN, VNEXT, classify_database
from scripts.upgrade.existing_vnext_upgrade import (
    capture_vnext_authoritative_snapshot, compare_vnext_authoritative_facts,
    upgrade_existing_vnext,
)
from scripts.upgrade.migration_runner import (
    apply_vnext_schema_v3, create_sqlite_schema,
)


class ExistingVNextUpgradeTest(unittest.TestCase):
    def _connection(self, old_schema=False):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        create_sqlite_schema(connection)
        connection.executescript("""
            INSERT INTO coverage_projects(
                project_name, created_at, updated_at
            ) VALUES ('FOS_V6R2', '2026-09-03 00:00:00', '2026-09-03 00:00:00');
            INSERT INTO coverage_scans(
                project_id, scan_key, scan_type, review_scope, info_file_name,
                info_sha256, imported_at, status, legacy_migrated, metadata_version
            ) VALUES (1, 'scan-existing-vnext', 'full', 'all', '', '',
                      '2026-09-03 00:00:00', 'ready', 0, 1);
            INSERT INTO coverage_files(
                scan_id, repository_name, file_path_hash, file_path, source_file_name
            ) VALUES (1, 'repo-a', 'file-a', 'src/a.c', 'a.c');
            INSERT INTO coverage_lines(
                file_id, line_number, line_text, coverage_state,
                block_start_line, block_end_line, block_type, function_name,
                function_hash, code_line_hash, code_occurrence, suggested_reviewer
            ) VALUES (1, 10, 'return 0;', 'uncovered', 10, 10, 'single',
                      'main', 'function-a', 'line-a', 1, '');
            INSERT INTO coverage_lines(
                file_id, line_number, line_text, coverage_state,
                block_start_line, block_end_line, block_type, function_name,
                function_hash, code_line_hash, code_occurrence, suggested_reviewer
            ) VALUES (1, 11, 'return 1;', 'covered', 11, 11, 'single',
                      'main', 'function-b', 'line-b', 1, '');
            INSERT INTO coverage_analyses(
                line_id, status, is_draft, reviewer, coverage_method,
                uncovered_reason, comment, created_at, updated_at
            ) VALUES (2, '可覆盖', 0, 'operator', 'unit', '', '',
                      '2026-09-03 00:00:00', '2026-09-03 00:00:00');
            INSERT INTO coverage_project_state(
                project_id, current_scan_id, data_version, file_state_version, updated_at
            ) VALUES (1, 1, 7, 7, '2026-09-03 00:00:00');
            INSERT INTO coverage_file_state(
                scan_id, file_id, total_lines, total_uncovered, filled_total,
                draft_total, confirmed_total, pending_total,
                ordinary_pending_total, inherited_pending_total,
                manual_draft_pending_total, data_version, updated_at
            ) VALUES (1, 1, 2, 1, 0, 0, 0, 0, 0, 0, 0, 7,
                      '2026-09-03 00:00:00');
        """)
        if old_schema:
            # This is the production-shaped pre-v3 condition: the business
            # facts exist, but the runtime report mode is not yet present.
            connection.execute("ALTER TABLE coverage_reports DROP COLUMN report_mode")
        connection.commit()
        self.addCleanup(connection.close)
        return connection

    def test_existing_vnext_rebuilds_file_state_and_adds_runtime_v3(self):
        source = self._connection()
        target = self._connection(old_schema=True)
        self.assertEqual(classify_database(source), VNEXT)
        self.assertEqual(classify_database(target), VNEXT)
        source_before = capture_vnext_authoritative_snapshot(source)

        result = upgrade_existing_vnext(
            source, target, release_sha="a" * 40,
            schema_path=os.path.join(
                os.getcwd(), "scripts", "upgrade", "vnext_schema_v3.sql"
            ),
        )

        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["schema_migration"]["status"], "PASSED")
        self.assertFalse(result["schema_migration"]["idempotent"])
        self.assertTrue(target.execute(
            "SELECT 1 FROM pragma_table_info('coverage_reports') "
            "WHERE name='report_mode'"
        ).fetchone())
        state = target.execute("""
            SELECT pending_total, ordinary_pending_total,
                   inherited_pending_total, manual_draft_pending_total,
                   data_version
            FROM coverage_file_state
            WHERE scan_id=1 AND file_id=1
        """).fetchone()
        self.assertEqual(tuple(state), (1, 1, 0, 0, 7))
        self.assertEqual(
            target.execute(
                "SELECT file_state_version FROM coverage_project_state WHERE project_id=1"
            ).fetchone()[0],
            7,
        )
        self.assertEqual(
            capture_vnext_authoritative_snapshot(source)["semantic_hash"],
            source_before["semantic_hash"],
        )
        self.assertEqual(
            result["authoritative_data_integrity"]["status"], "PASSED"
        )

        rows_before = target.execute(
            "SELECT COUNT(*) FROM coverage_analysis_records"
        ).fetchone()[0]
        version_before = target.execute(
            "SELECT data_version FROM coverage_project_state WHERE project_id=1"
        ).fetchone()[0]
        second = upgrade_existing_vnext(
            source, target, release_sha="b" * 40,
            schema_path=os.path.join(
                os.getcwd(), "scripts", "upgrade", "vnext_schema_v3.sql"
            ),
        )
        self.assertTrue(second["idempotent_schema"])
        self.assertEqual(second["schema_migration"]["operations_applied"], 0)
        self.assertEqual(
            target.execute("SELECT COUNT(*) FROM coverage_analysis_records").fetchone()[0],
            rows_before,
        )
        self.assertEqual(
            target.execute(
                "SELECT data_version FROM coverage_project_state WHERE project_id=1"
            ).fetchone()[0],
            version_before,
        )

    def test_existing_vnext_rejects_non_consistent_target_before_ddl(self):
        source = self._connection()
        target = self._connection(old_schema=True)
        target.execute(
            "UPDATE coverage_files SET file_path='src/other.c' WHERE id=1"
        )
        target.commit()
        with self.assertRaisesRegex(ValueError, "consistent source backup"):
            upgrade_existing_vnext(source, target)
        self.assertIsNone(target.execute(
            "SELECT 1 FROM pragma_table_info('coverage_reports') "
            "WHERE name='report_mode'"
        ).fetchone())

    def test_semantic_snapshot_ignores_auto_increment_ids(self):
        source = self._connection()
        target = self._connection(old_schema=True)
        target.executescript("""
            PRAGMA foreign_keys=OFF;
            UPDATE coverage_projects SET id=101 WHERE id=1;
            UPDATE coverage_scans SET id=203, project_id=101 WHERE id=1;
            UPDATE coverage_files SET id=307, scan_id=203 WHERE id=1;
            UPDATE coverage_lines SET id=401, file_id=307 WHERE id=1;
            UPDATE coverage_lines SET id=402, file_id=307 WHERE id=2;
            UPDATE coverage_analyses SET id=505, line_id=402 WHERE id=1;
            UPDATE coverage_project_state
            SET project_id=101, current_scan_id=203 WHERE project_id=1;
            UPDATE coverage_file_state
            SET scan_id=203, file_id=307 WHERE scan_id=1 AND file_id=1;
            PRAGMA foreign_keys=ON;
        """)
        target.commit()
        comparison = compare_vnext_authoritative_facts(
            capture_vnext_authoritative_snapshot(source),
            capture_vnext_authoritative_snapshot(target),
        )
        self.assertEqual(comparison["status"], "PASSED")
        self.assertEqual(comparison["differences"], [])

    def test_semantic_snapshot_normalizes_relation_ids_and_runtime_state(self):
        source = self._connection()
        target = self._connection(old_schema=True)

        def seed(connection, ids):
            stamp = "2026-09-03 00:00:00"
            connection.execute(
                """INSERT INTO coverage_scans(
                    id, project_id, scan_key, scan_type, review_scope,
                    info_file_name, info_sha256, imported_at, status,
                    legacy_migrated, metadata_version, predecessor_scan_id,
                    algorithm_version
                ) VALUES (?, ?, ?, 'incremental', 'all', '', '', ?, 'ready',
                          0, 1, ?, 'algorithm-v1')""",
                (ids["predecessor"], ids["project"], "scan-predecessor",
                 stamp, None),
            )
            connection.execute(
                "UPDATE coverage_scans SET predecessor_scan_id=? WHERE id=?",
                (ids["predecessor"], ids["scan"]),
            )
            connection.execute(
                """INSERT INTO coverage_repository_resources(
                    id, resource_key, resolved_git_common_dir,
                    resolved_worktree_root, fs_device, fs_inode,
                    next_fencing_token, observed_at
                ) VALUES (?, 'resource-key', '/git/common', '/worktree',
                          1, 2, 3, ?)""",
                (ids["resource"], stamp),
            )
            connection.execute(
                """INSERT INTO coverage_repositories(
                    id, project_id, repository_name, canonical_remote,
                    last_observed_physical_path, physical_resource_id,
                    lifecycle_state, created_at, updated_at
                ) VALUES (?, ?, 'repo-a', 'https://example.invalid/repo.git',
                          '/worktree', ?, 'ACTIVE', ?, ?)""",
                (ids["repository"], ids["project"], ids["resource"], stamp, stamp),
            )
            connection.execute(
                """INSERT INTO coverage_repository_aliases(
                    id, project_id, repository_id, alias_name, created_at
                ) VALUES (?, ?, ?, 'repo-alias', ?)""",
                (ids["alias"], ids["project"], ids["repository"], stamp),
            )
            connection.execute(
                """INSERT INTO coverage_scan_repositories(
                    id, scan_id, repository_name, repository_path, branch_name,
                    old_commit_sha, new_commit_sha, verified, captured_at,
                    provenance, repository_id, commit_sha, identity_verified,
                    identity_provenance
                ) VALUES (?, ?, 'repo-a', '/worktree', 'main', 'a', 'b', 1,
                          ?, 'operator', ?, 'c', 1, 'git')""",
                (ids["scan_repo"], ids["scan"], stamp, ids["repository"]),
            )
            connection.execute(
                """INSERT INTO coverage_analysis_records(
                    id, conclusion_status, coverage_method, content_hash,
                    content_origin, legacy_source_analysis_id, created_at, updated_at
                ) VALUES (?, 'PENDING', 'manual', 'record-hash', 'MANUAL', ?, ?, ?)""",
                (ids["record"], ids["analysis"], stamp, stamp),
            )
            connection.execute(
                """INSERT INTO coverage_analysis_blocks(
                    id, scan_id, repository_id, file_id, start_line, end_line,
                    origin, block_identity_verified, originating_record_id,
                    initial_content_hash, created_by, created_at
                ) VALUES (?, ?, ?, ?, 1, 1, 'MANUAL', 1, ?, 'block-hash',
                          'operator', ?)""",
                (ids["block"], ids["scan"], ids["repository"], ids["file"],
                 ids["record"], stamp),
            )
            connection.execute(
                """INSERT INTO coverage_inheritance_groups(
                    id, decision_run_id, candidate_scan_id, source_scan_id,
                    source_analysis_block_id, repository_id, candidate_file_id,
                    mapping_fingerprint, created_at
                ) VALUES (?, 'decision-run', ?, ?, ?, ?, ?, 'mapping', ?)""",
                (ids["group"], ids["scan"], ids["predecessor"], ids["block"],
                 ids["repository"], ids["file"], stamp),
            )
            connection.execute(
                """INSERT INTO coverage_analysis_line_links(
                    id, scan_id, line_id, analysis_record_id, analysis_block_id,
                    review_state, relation_origin, inheritance_group_id,
                    is_active, reviewed_by, reviewed_at, source_scan_id,
                    source_line_id, relation_revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'INHERITED_PENDING', 'INHERITED', ?,
                          1, 'operator', ?, ?, ?, 1, ?, ?)""",
                (ids["link"], ids["scan"], ids["line"], ids["record"],
                 ids["block"], ids["group"], stamp, ids["predecessor"],
                 ids["line2"], stamp, stamp),
            )
            connection.execute(
                """INSERT INTO coverage_inheritance_decisions(
                    id, decision_run_id, candidate_scan_id, candidate_line_id,
                    source_scan_id, source_line_id, source_relation_id, decision,
                    reason_code, algorithm_version, evaluated_at
                ) VALUES (?, 'decision-run', ?, ?, ?, ?, ?, 'INHERITED',
                          'MATCH', 'algorithm-v1', ?)""",
                (ids["decision"], ids["scan"], ids["line"], ids["predecessor"],
                 ids["line2"], ids["link"], stamp),
            )
            connection.execute(
                """INSERT INTO coverage_inheritance_rejections(
                    id, scan_id, line_id, rejected_relation_id,
                    rejected_relation_revision, rejected_analysis_record_id,
                    rejected_source_scan_id, rejected_source_line_id,
                    rejected_source_relation_id, rejection_revision, is_active,
                    terminal_reason, rejected_by, rejected_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, 1, 1, 'superseded',
                          'operator', ?)""",
                (ids["rejection"], ids["scan"], ids["line"], ids["link"],
                 ids["record"], ids["predecessor"], ids["line2"], ids["link"], stamp),
            )
            connection.execute(
                """INSERT INTO coverage_background_jobs(
                    job_id, project_id, scan_id, kind, state, progress,
                    input_payload, result_path, error_message, data_version,
                    handler_version, heartbeat_at, lease_owner, created_at,
                    started_at, finished_at, updated_at, legacy_raw_percent,
                    legacy_percent_unit
                ) VALUES ('job-runtime', ?, ?, 'import', 'completed', 1,
                          '{}', '', NULL, 7, 'v1', ?, '', ?, ?, ?, ?, 100, 'percent')""",
                (ids["project"], ids["scan"], stamp, stamp, stamp, stamp, stamp),
            )
            connection.execute(
                """INSERT INTO coverage_incremental_results(
                    id, scan_id, report_id, repository_name, incremental_key_hash,
                    old_commit_sha, new_commit_sha, payload, generated_at
                ) VALUES (?, ?, 'report', 'repo-a', 'incremental-key', 'a', 'b',
                          '{}', ?)""",
                (ids["incremental"], ids["scan"], stamp),
            )
            connection.execute(
                """INSERT INTO coverage_import_artifacts(
                    artifact_id, job_id, kind, staged_path, sha256, size_bytes,
                    immutable, created_at
                ) VALUES ('artifact-runtime', 'job-runtime', 'input',
                          '/staged/input', 'artifact-hash', 1, 1, ?)""",
                (stamp,),
            )
            connection.execute(
                """INSERT INTO coverage_import_checkpoints(
                    job_id, scan_id, phase, payload, expected_current_scan_id,
                    created_at, updated_at
                ) VALUES ('job-runtime', ?, 'parse', '{}', ?, ?, ?)""",
                (ids["scan"], ids["scan"], stamp, stamp),
            )
            connection.execute(
                """INSERT INTO coverage_import_failures(
                    id, job_id, scan_id, phase, error_class, error_fingerprint,
                    failure_key_hash, message_redacted, occurred_at
                ) VALUES (?, 'job-runtime', ?, 'parse', 'ParseError',
                          'error-fingerprint', 'failure-key', 'redacted', ?)""",
                (ids["failure"], ids["scan"], stamp),
            )
            connection.execute(
                """INSERT INTO coverage_migration_checkpoints(
                    migration_id, checkpoint_key, checkpoint_key_hash, phase,
                    target_counts, state, updated_at
                ) VALUES ('migration-runtime', 'checkpoint', 'checkpoint-hash',
                          'facts', '{}', 'APPLIED', ?)""",
                (stamp,),
            )
            provenance = (
                ("line", ids["line"], "coverage_line_index", "line-source"),
                ("legacy_analysis", ids["analysis"], "coverage_analysis", "analysis-source"),
                ("project_state", ids["project"], "coverage_project_state", "FOS_V6R2"),
                ("job", 12345, "coverage_background_jobs", "job-runtime"),
            )
            for index, (entity_type, entity_id, table, identity) in enumerate(provenance):
                connection.execute(
                    """INSERT INTO coverage_legacy_provenance(
                        id, migration_id, target_entity_type, target_entity_id,
                        source_table, source_identity, provenance_key_hash,
                        raw_payload_sha256, created_at
                    ) VALUES (?, 'migration-runtime', ?, ?, ?, ?, ?, 'raw-hash', ?)""",
                    (ids["provenance"] + index, entity_type, entity_id, table,
                     identity, "provenance-key-{}".format(index), stamp),
                )
            connection.execute(
                """INSERT INTO coverage_repository_resource_locks(
                    physical_resource_id, job_id, owner_token, fencing_token,
                    heartbeat_at, acquired_at
                ) VALUES (?, 'job-runtime', 'owner', 1, ?, ?)""",
                (ids["resource"], stamp, stamp),
            )
            connection.commit()

        seed(source, {
            "project": 1, "scan": 1, "predecessor": 2, "resource": 1,
            "repository": 1, "alias": 1, "scan_repo": 1, "record": 1,
            "analysis": 1, "block": 1, "group": 1, "link": 1,
            "decision": 1, "rejection": 1, "incremental": 1,
            "failure": 1, "provenance": 1, "file": 1, "line": 1, "line2": 2,
        })
        target.executescript("""
            PRAGMA foreign_keys=OFF;
            UPDATE coverage_projects SET id=101 WHERE id=1;
            UPDATE coverage_scans SET id=203, project_id=101 WHERE id=1;
            UPDATE coverage_files SET id=307, scan_id=203 WHERE id=1;
            UPDATE coverage_lines SET id=401, file_id=307 WHERE id=1;
            UPDATE coverage_lines SET id=402, file_id=307 WHERE id=2;
            UPDATE coverage_analyses SET id=505, line_id=402 WHERE id=1;
            UPDATE coverage_project_state
            SET project_id=101, current_scan_id=203 WHERE project_id=1;
            UPDATE coverage_file_state
            SET scan_id=203, file_id=307 WHERE scan_id=1 AND file_id=1;
            PRAGMA foreign_keys=ON;
        """)
        seed(target, {
            "project": 101, "scan": 203, "predecessor": 204, "resource": 901,
            "repository": 902, "alias": 903, "scan_repo": 904, "record": 905,
            "analysis": 505, "block": 906, "group": 907, "link": 908,
            "decision": 909, "rejection": 910, "incremental": 911,
            "failure": 912, "provenance": 913, "file": 307, "line": 401, "line2": 402,
        })

        before = compare_vnext_authoritative_facts(
            capture_vnext_authoritative_snapshot(source),
            capture_vnext_authoritative_snapshot(target),
        )
        self.assertEqual(before["status"], "PASSED", before)
        result = upgrade_existing_vnext(source, target, release_sha="a" * 40)
        self.assertEqual(result["status"], "PASSED")
        after = compare_vnext_authoritative_facts(
            capture_vnext_authoritative_snapshot(source),
            capture_vnext_authoritative_snapshot(target),
        )
        self.assertEqual(after["status"], "PASSED", after)

    def test_database_generation_is_fail_closed_for_legacy_and_unknown(self):
        legacy = sqlite3.connect(":memory:")
        self.addCleanup(legacy.close)
        legacy.executescript("""
            CREATE TABLE coverage_line_index(id INTEGER);
            CREATE TABLE coverage_analysis(id INTEGER);
        """)
        self.assertEqual(classify_database(legacy), LEGACY)
        unknown = sqlite3.connect(":memory:")
        self.addCleanup(unknown.close)
        unknown.execute("CREATE TABLE operator_scratch(value TEXT)")
        self.assertEqual(classify_database(unknown), UNKNOWN)

        ambiguous = sqlite3.connect(":memory:")
        self.addCleanup(ambiguous.close)
        ambiguous.executescript("""
            CREATE TABLE coverage_line_index(id INTEGER);
            CREATE TABLE coverage_analysis(id INTEGER);
            CREATE TABLE coverage_projects(id INTEGER);
            CREATE TABLE coverage_scans(id INTEGER);
            CREATE TABLE coverage_lines(id INTEGER);
            CREATE TABLE coverage_project_state(id INTEGER);
        """)
        self.assertEqual(classify_database(ambiguous), UNKNOWN)

    def test_v3_checksum_drift_after_applied_fails_closed(self):
        target = self._connection(old_schema=True)
        schema_path = os.path.join(
            os.getcwd(), "scripts", "upgrade", "vnext_schema_v3.sql"
        )
        apply_vnext_schema_v3(target, schema_path, release_sha="a" * 40)
        with open(schema_path, "r", encoding="utf-8") as stream:
            ddl = stream.read()
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".sql") as stream:
            stream.write(ddl + "\n-- reviewed file changed after apply\n")
            stream.flush()
            with self.assertRaisesRegex(ValueError, "checksum changed after APPLIED"):
                apply_vnext_schema_v3(target, stream.name, release_sha="b" * 40)


if __name__ == "__main__":
    unittest.main()
