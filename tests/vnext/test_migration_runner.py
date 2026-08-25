import hashlib
import os
import sqlite3
import tempfile
from datetime import datetime
import unittest
from unittest import mock

from scripts.upgrade.migration_runner import (
    capture_legacy_snapshot,
    capture_legacy_semantic_snapshot,
    capture_vnext_snapshot,
    capture_vnext_semantic_snapshot,
    create_sqlite_schema,
    migrate_legacy,
    semantic_hash,
    SEMANTIC_HASH_VERSION,
    canonical_semantic_component_hash,
    canonical_semantic_component_hashes,
    canonical_semantic_contract_hash,
    _stream_legacy_semantic_hash,
    _stream_vnext_semantic_hash,
    _legacy_file_contexts,
    _migration_checkpoint_done,
    _migration_checkpoint_key_hash,
    _upsert_migration_checkpoint,
    _split_sql,
)
from scripts.upgrade.schema_preflight import (
    PROTECTED_TABLES, analyze_sql_script, validate_ddl_file,
)


def legacy_connection():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE coverage_analysis (
            id INTEGER PRIMARY KEY, project_name TEXT, file_path TEXT,
            file_path_hash TEXT, source_file_name TEXT, line_number INTEGER,
            reviewer TEXT, status TEXT, is_draft INTEGER, coverage_method TEXT,
            uncovered_reason TEXT, comment TEXT
        );
        CREATE TABLE coverage_line_index (
            id INTEGER PRIMARY KEY, project_name TEXT, file_path TEXT,
            file_path_hash TEXT, source_file_name TEXT, line_number INTEGER,
            line_text TEXT, block_start_line INTEGER, block_end_line INTEGER,
            block_type TEXT, function_name TEXT, function_hash TEXT,
            code_line_hash TEXT, code_occurrence INTEGER
        );
        CREATE TABLE coverage_project_state (
            project_name TEXT PRIMARY KEY, data_version INTEGER, updated_at TEXT,
            file_state_version INTEGER
        );
        CREATE TABLE coverage_background_jobs (
            job_id TEXT PRIMARY KEY, kind TEXT, project_name TEXT,
            data_version INTEGER, state TEXT, error_message TEXT
        );
    """)
    path = "src/migrated.c"
    file_hash = hashlib.md5(path.encode("utf-8")).hexdigest()
    connection.execute(
        "INSERT INTO coverage_project_state VALUES (?, ?, ?, ?)",
        ("project-a", 7, "2026-08-20 00:00:00", 7),
    )
    connection.execute(
        "INSERT INTO coverage_line_index VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, "project-a", path, file_hash, "migrated.c", 10, "return 0;",
         10, 10, "single", "main", "fn", "line", 1),
    )
    connection.execute(
        "INSERT INTO coverage_analysis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, "project-a", path, file_hash, "migrated.c", 10, "alice",
         "可覆盖", 0, "unit", "", "legacy comment"),
    )
    connection.execute(
        "INSERT INTO coverage_background_jobs VALUES (?, ?, ?, ?, ?, ?)",
        ("job-1", "export", "project-a", 7, "completed", ""),
    )
    connection.commit()
    return connection


def semantic_fixture():
    return {
        "projects": ["FOSv6R2", "FOS_V6R2", "Gemini-NOS"],
        "project_data_versions": [
            {"project_name": "FOSv6R2", "data_version": 1},
            {"project_name": "FOS_V6R2", "data_version": 1},
            {"project_name": "Gemini-NOS", "data_version": 1},
        ],
        "lines": [],
        "analyses": [],
        "jobs": [],
        "legacy_provenance": [],
    }


class MigrationRunnerTest(unittest.TestCase):
    def test_checkpoint_identity_hash_handles_long_unicode_keys(self):
        target = sqlite3.connect(":memory:")
        target.row_factory = sqlite3.Row
        self.addCleanup(target.close)
        create_sqlite_schema(target)

        migration_id = "legacy-v3-unicode"
        checkpoint_key = "file:{}:{}:{}".format(
            "项目" * 30, "a" * 32, "路径/源文件.c" * 80,
        )
        _upsert_migration_checkpoint(
            target, migration_id, checkpoint_key, "FILE",
            source_cursor=checkpoint_key, semantic_fragment_hash="b" * 64,
        )
        target.commit()

        self.assertEqual(
            _migration_checkpoint_key_hash(migration_id, checkpoint_key),
            target.execute(
                "SELECT checkpoint_key_hash FROM coverage_migration_checkpoints"
            ).fetchone()[0],
        )
        self.assertTrue(_migration_checkpoint_done(
            target, migration_id, checkpoint_key, "b" * 64,
        ))
        saved = target.execute(
            "SELECT checkpoint_key FROM coverage_migration_checkpoints"
        ).fetchone()[0]
        self.assertEqual(saved, checkpoint_key)

    def test_semantic_hash_normalizes_mariadb_datetime_values(self):
        self.assertEqual(
            semantic_hash({
                "legacy_provenance": [{
                    "legacy_created_at": datetime(2026, 8, 21, 12, 34, 56),
                }],
            }),
            semantic_hash({
                "legacy_provenance": [{
                    "legacy_created_at": "2026-08-21 12:34:56",
                }],
            }),
        )

    def test_canonical_multiset_ignores_database_return_order(self):
        source = semantic_fixture()
        target = semantic_fixture()
        target["projects"].reverse()
        target["project_data_versions"].reverse()
        self.assertEqual(
            canonical_semantic_contract_hash(source),
            canonical_semantic_contract_hash(target),
        )
        self.assertEqual(
            canonical_semantic_component_hashes(source),
            canonical_semantic_component_hashes(target),
        )
        self.assertEqual(SEMANTIC_HASH_VERSION, "canonical-multiset-v2")

    def test_canonical_multiset_detects_data_version_change(self):
        source = semantic_fixture()
        target = semantic_fixture()
        target["project_data_versions"][0]["data_version"] = 2
        source_components = canonical_semantic_component_hashes(source)
        target_components = canonical_semantic_component_hashes(target)
        self.assertNotEqual(
            source_components["project_data_versions"],
            target_components["project_data_versions"],
        )
        self.assertNotEqual(
            canonical_semantic_contract_hash(source),
            canonical_semantic_contract_hash(target),
        )

    def test_canonical_multiset_detects_analysis_fact_changes(self):
        analysis = {
            "project_name": "FOSv6R2", "file_path_hash": "f" * 32,
            "file_path": "src/a.c", "line_number": 10, "status": "可覆盖",
            "is_draft": 0, "reviewer": "alice", "coverage_method": "unit",
            "uncovered_reason": "", "comment": "original",
        }
        for field, value in (
                ("status", "未确认"), ("reviewer", "bob"),
                ("coverage_method", "manual"),
                ("uncovered_reason", "needs review"),
                ("comment", "changed")):
            with self.subTest(field=field):
                source = semantic_fixture()
                target = semantic_fixture()
                source["analyses"] = [dict(analysis)]
                target["analyses"] = [dict(analysis)]
                target["analyses"][0][field] = value
                self.assertNotEqual(
                    canonical_semantic_contract_hash(source),
                    canonical_semantic_contract_hash(target),
                )

    def test_canonical_multiset_preserves_duplicate_count(self):
        record_a = {"project_name": "A"}
        record_b = {"project_name": "B"}
        self.assertNotEqual(
            canonical_semantic_component_hash(
                [record_a, record_a, record_b], chunk_size=1
            ),
            canonical_semantic_component_hash(
                [record_a, record_b], chunk_size=1
            ),
        )

    def test_canonical_multiset_handles_unicode_case_and_symbols(self):
        names = [
            "FOSv6R2", "FOS_V6R2", "fosv6r2",
            "项目A", "项目_A", "项目-A",
        ]
        source = semantic_fixture()
        source["projects"] = names
        source["project_data_versions"] = [
            {"project_name": name, "data_version": index}
            for index, name in enumerate(names, 1)
        ]
        target = semantic_fixture()
        target["projects"] = list(reversed(names))
        target["project_data_versions"] = list(reversed(
            source["project_data_versions"]
        ))
        self.assertEqual(
            canonical_semantic_contract_hash(source),
            canonical_semantic_contract_hash(target),
        )

    def test_canonical_multiset_chunked_hash_matches_snapshot_hash(self):
        snapshot = semantic_fixture()
        snapshot["projects"] = [
            "FOSv6R2", "FOS_V6R2", "Gemini-NOS", "项目A",
        ]
        self.assertEqual(
            canonical_semantic_contract_hash(snapshot, chunk_size=1),
            canonical_semantic_contract_hash(snapshot, chunk_size=50000),
        )

    def test_legacy_timestamp_columns_keep_microsecond_precision(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
        path = os.path.join(root, "scripts", "upgrade", "vnext_schema.sql")
        with open(path, "r", encoding="utf-8") as stream:
            ddl = stream.read().lower()
        self.assertIn("legacy_created_at datetime(6) null", ddl)
        self.assertIn("legacy_updated_at datetime(6) null", ddl)
        self.assertIn("legacy_source_created_at datetime(6) null", ddl)
        self.assertIn("legacy_source_updated_at datetime(6) null", ddl)

    def test_domain_constraint_ddl_is_additive_and_explicitly_restrictive(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
        path = os.path.join(root, "scripts", "upgrade", "vnext_domain_constraints.sql")
        safe, errors, warnings = validate_ddl_file(path)
        self.assertTrue(safe, errors)
        self.assertEqual(warnings, [])
        with open(path, "r", encoding="utf-8") as stream:
            ddl = stream.read().upper()
        self.assertIn("FK_COVERAGE_REPOSITORIES_RESOURCE", ddl)
        self.assertIn("FK_ANALYSIS_LINE_LINK_SOURCE_RELATION", ddl)
        self.assertNotIn("ON DELETE CASCADE", ddl)

    def test_gate_a_and_domain_tables_are_protected_from_destructive_ddl(self):
        for table in (
            "coverage_schema_migrations", "coverage_analysis_records",
            "coverage_analysis_line_links", "coverage_import_artifacts",
            "coverage_import_checkpoints", "coverage_import_failures",
        ):
            self.assertIn(table, PROTECTED_TABLES)
            safe, errors, _ = analyze_sql_script("DROP TABLE {};".format(table))
            self.assertFalse(safe)
            self.assertTrue(errors)

    def test_sql_splitter_strips_comments_without_splitting_string_literals(self):
        statements = _split_sql(
            "-- schema header;\n"
            "CREATE TABLE audit (value VARCHAR(32) COMMENT 'a;b');\n"
            "/* block comment; */ INSERT INTO audit VALUES ('x;y');"
        )
        self.assertEqual(len(statements), 2)
        self.assertTrue(statements[0].startswith("CREATE TABLE audit"))
        self.assertNotIn("schema header", statements[0])
        self.assertEqual(statements[1], "INSERT INTO audit VALUES ('x;y')")

    def test_migration_preserves_facts_and_is_idempotent(self):
        source = legacy_connection()
        self.addCleanup(source.close)
        target = sqlite3.connect(":memory:")
        self.addCleanup(target.close)
        target.row_factory = sqlite3.Row
        create_sqlite_schema(target)
        before = capture_legacy_snapshot(source)
        with mock.patch(
                "scripts.upgrade.migration_runner._legacy_file_contexts",
                wraps=_legacy_file_contexts) as context_reader:
            first = migrate_legacy(source, target)
        self.assertEqual(
            context_reader.call_count, 1,
            "the migration hash and write passes must share one source context read",
        )
        self.assertEqual(first["status"], "PASSED")
        self.assertEqual(first["source_line_facts"], 1)
        self.assertEqual(first["source_analysis_facts"], 1)
        self.assertEqual(first["anomalies"], [])
        self.assertTrue(first["authoritative_semantic_match"])
        self.assertEqual(first["semantic_hash_version"], SEMANTIC_HASH_VERSION)
        self.assertEqual(
            first["source_component_hashes"],
            first["target_component_hashes"],
        )
        self.assertEqual(first["semantic_mismatch_components"], [])
        self.assertEqual(
            first["target_semantic_hash"],
            _stream_vnext_semantic_hash(target, batch_size=1),
        )
        self.assertEqual(
            first["target_semantic_hash"],
            canonical_semantic_contract_hash(
                capture_vnext_semantic_snapshot(target), chunk_size=1
            ),
        )
        project = target.execute(
            "SELECT project_name FROM coverage_projects"
        ).fetchall()
        self.assertEqual([row[0] for row in project], ["project-a"])
        self.assertEqual(
            target.execute("SELECT data_version FROM coverage_project_state").fetchone()[0],
            7,
        )
        second = migrate_legacy(source, target)
        self.assertEqual(second["source_line_facts"], 1)
        self.assertEqual(target.execute("SELECT COUNT(*) FROM coverage_scans").fetchone()[0], 1)
        self.assertEqual(target.execute("SELECT COUNT(*) FROM coverage_lines").fetchone()[0], 1)
        self.assertEqual(target.execute("SELECT COUNT(*) FROM coverage_analyses").fetchone()[0], 1)
        self.assertEqual(
            target.execute("SELECT COUNT(*) FROM coverage_background_jobs").fetchone()[0], 1
        )
        checkpoint = target.execute(
            "SELECT phase, state FROM coverage_migration_checkpoints "
            "WHERE checkpoint_key=?",
            ("project:project-a",),
        ).fetchone()
        self.assertEqual(tuple(checkpoint), ("PUBLISHED", "PUBLISHED"))
        after = capture_vnext_snapshot(target)
        self.assertEqual(after["projects"], before["projects"])
        self.assertEqual(after["project_data_versions"], before["project_data_versions"])
        self.assertEqual(len(after["lines"]), len(before["lines"]))
        self.assertEqual(len(after["analyses"]), len(before["analyses"]))

    def test_gate_reports_changed_project_data_version_component(self):
        source = legacy_connection()
        self.addCleanup(source.close)
        target = sqlite3.connect(":memory:")
        self.addCleanup(target.close)
        target.row_factory = sqlite3.Row
        create_sqlite_schema(target)
        first = migrate_legacy(source, target)
        self.assertEqual(first["status"], "PASSED")
        target.execute(
            "UPDATE coverage_project_state SET data_version= data_version + 1"
        )
        target.commit()
        second = migrate_legacy(source, target)
        self.assertEqual(second["status"], "FAILED")
        self.assertEqual(
            second["semantic_mismatch_components"],
            ["project_data_versions"],
        )
        self.assertFalse(second["authoritative_semantic_match"])

    def test_legacy_semantic_hash_scans_each_file_context_once(self):
        source = legacy_connection()
        self.addCleanup(source.close)
        path = "src/ordered.c"
        file_hash = hashlib.md5(path.encode("utf-8")).hexdigest()
        source.execute(
            "INSERT INTO coverage_line_index VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (2, "project-a", path, file_hash, "migrated.c", 2, "return 2;",
             2, 2, "single", "main", "fn", "line-2", 1),
        )
        source.execute(
            "INSERT INTO coverage_line_index VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (3, "project-a", path, file_hash, "migrated.c", 10, "return 10;",
             10, 10, "single", "main", "fn", "line-10", 1),
        )
        source.execute(
            "INSERT INTO coverage_analysis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (2, "project-a", path, file_hash, "migrated.c", 2, "alice",
             "可覆盖", 0, "unit", "", "comment-2"),
        )
        source.execute(
            "INSERT INTO coverage_analysis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (3, "project-a", path, file_hash, "migrated.c", 10, "alice",
             "可覆盖", 0, "unit", "", "comment-10"),
        )
        source.commit()
        expected = canonical_semantic_contract_hash(
            capture_legacy_semantic_snapshot(source)
        )
        with mock.patch(
                "scripts.upgrade.migration_runner._legacy_file_contexts",
                wraps=_legacy_file_contexts) as context_reader:
            descriptor = _stream_legacy_semantic_hash(
                source, ["project-a"], {"project-a": 7},
            )
        self.assertEqual(descriptor["semantic_hash"], expected)
        self.assertEqual(
            descriptor["component_hashes"],
            canonical_semantic_component_hashes(
                capture_legacy_semantic_snapshot(source)
            ),
        )
        self.assertEqual(
            context_reader.call_count, 2,
            "one legacy file context read per physical file is expected",
        )

    def test_blank_analysis_path_uses_unique_line_identity(self):
        source = legacy_connection()
        self.addCleanup(source.close)
        source.execute(
            "UPDATE coverage_analysis SET file_path=? WHERE id=?",
            ("", 1),
        )
        source.commit()
        target = sqlite3.connect(":memory:")
        self.addCleanup(target.close)
        target.row_factory = sqlite3.Row
        create_sqlite_schema(target)
        result = migrate_legacy(source, target)
        self.assertTrue(result["authoritative_semantic_match"])
        self.assertEqual(
            target.execute("SELECT file_path FROM coverage_files").fetchone()[0],
            "src/migrated.c",
        )

    def test_missing_file_path_on_first_legacy_line_uses_hash_context(self):
        source = legacy_connection()
        self.addCleanup(source.close)
        file_hash = "m" * 32
        source.execute(
            "INSERT INTO coverage_line_index VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (9, "project-a", "", file_hash, "missing.c", 9, "return 9;",
             9, 9, "single", "main", "fn", "line-9", 1),
        )
        source.commit()
        contexts = _legacy_file_contexts(source, "project-a", file_hash)
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0]["file_path"], file_hash)
        self.assertEqual(contexts[0]["missing_file_path_lines"], [9])

    def test_missing_file_path_in_middle_is_recorded_on_fallback_context(self):
        source = legacy_connection()
        self.addCleanup(source.close)
        file_hash = "n" * 32
        for row in (
            (10, "src/known.c", 10, "known"),
            (11, "", 11, "missing"),
        ):
            source.execute(
                "INSERT INTO coverage_line_index VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row[0], "project-a", row[1], file_hash, "known.c", row[2],
                 row[3], row[2], row[2], "single", "main", "fn",
                 "line-{}".format(row[2]), 1),
            )
        source.commit()
        contexts = _legacy_file_contexts(source, "project-a", file_hash)
        by_path = {item["file_path"]: item for item in contexts}
        self.assertEqual(by_path["src/known.c"]["missing_file_path_lines"], [])
        self.assertEqual(by_path[file_hash]["missing_file_path_lines"], [11])

    def test_active_jobs_and_analysis_only_lines_are_explicitly_mapped(self):
        source = legacy_connection()
        self.addCleanup(source.close)
        source.execute(
            "INSERT INTO coverage_analysis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (2, "project-a", "src/analysis_only.c",
             hashlib.md5(b"src/analysis_only.c").hexdigest(), "analysis_only.c", 22,
             "bob", "未确认", 1, "", "reason", ""),
        )
        source.execute(
            "INSERT INTO coverage_background_jobs VALUES (?, ?, ?, ?, ?, ?)",
            ("job-running", "export", "project-a", 7, "running", ""),
        )
        source.commit()
        target = sqlite3.connect(":memory:")
        self.addCleanup(target.close)
        target.row_factory = sqlite3.Row
        create_sqlite_schema(target)
        result = migrate_legacy(source, target)
        anomaly_types = [item["type"] for item in result["anomalies"]]
        self.assertIn("missing_line_index_context", anomaly_types)
        self.assertIn("active_job_requires_decision", anomaly_types)
        self.assertTrue(result["authoritative_semantic_match"])
        self.assertEqual(
            target.execute("SELECT COUNT(*) FROM coverage_analyses").fetchone()[0], 2
        )
        self.assertEqual(
            target.execute(
                "SELECT state FROM coverage_background_jobs WHERE job_id = ?",
                ("job-running",),
            ).fetchone()[0], "interrupted"
        )

    def test_same_legacy_hash_with_two_paths_is_namespaced_and_reported(self):
        source = legacy_connection()
        self.addCleanup(source.close)
        file_hash = "f" * 32
        source.execute(
            "INSERT INTO coverage_line_index VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (2, "project-a", "src/one.c", file_hash, "one.c", 10, "a", 10, 10,
             "single", "", "", "a", 1),
        )
        source.execute(
            "INSERT INTO coverage_line_index VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (3, "project-a", "src/two.c", file_hash, "two.c", 10, "b", 10, 10,
             "single", "", "", "b", 1),
        )
        source.commit()
        target = sqlite3.connect(":memory:")
        self.addCleanup(target.close)
        target.row_factory = sqlite3.Row
        create_sqlite_schema(target)
        result = migrate_legacy(source, target)
        self.assertIn("path_conflict", [item["type"] for item in result["anomalies"]])
        self.assertEqual(
            target.execute("SELECT COUNT(*) FROM coverage_files").fetchone()[0], 3
        )
        self.assertEqual(
            target.execute("SELECT COUNT(*) FROM coverage_lines").fetchone()[0], 3
        )


if __name__ == "__main__":
    unittest.main()
