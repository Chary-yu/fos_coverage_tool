import hashlib
import sqlite3
import tempfile
import unittest

from scripts.upgrade.migration_runner import (
    capture_legacy_snapshot,
    capture_vnext_snapshot,
    create_sqlite_schema,
    migrate_legacy,
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


class MigrationRunnerTest(unittest.TestCase):
    def test_migration_preserves_facts_and_is_idempotent(self):
        source = legacy_connection()
        target = sqlite3.connect(":memory:")
        target.row_factory = sqlite3.Row
        create_sqlite_schema(target)
        before = capture_legacy_snapshot(source)
        first = migrate_legacy(source, target)
        self.assertEqual(first["status"], "PASSED")
        self.assertEqual(first["source_line_facts"], 1)
        self.assertEqual(first["source_analysis_facts"], 1)
        self.assertEqual(first["anomalies"], [])
        self.assertTrue(first["authoritative_semantic_match"])
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
        after = capture_vnext_snapshot(target)
        self.assertEqual(after["projects"], before["projects"])
        self.assertEqual(after["project_data_versions"], before["project_data_versions"])
        self.assertEqual(len(after["lines"]), len(before["lines"]))
        self.assertEqual(len(after["analyses"]), len(before["analyses"]))


if __name__ == "__main__":
    unittest.main()
