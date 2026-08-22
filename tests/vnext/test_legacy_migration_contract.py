import os
import sqlite3
import tempfile
import unittest

from scripts.upgrade.database_identity import (
    assert_separate_connections, fingerprint_connection,
)
from scripts.upgrade.legacy_fixture import (
    create_legacy_fixture_schema, seed_legacy_fixture,
)
from scripts.upgrade.migration_runner import (
    apply_schema, assert_empty_vnext_target, create_sqlite_schema,
    migrate_legacy,
)


class LegacyMigrationContractTest(unittest.TestCase):
    def _source(self, **kwargs):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        create_legacy_fixture_schema(connection)
        seed_legacy_fixture(connection, **kwargs)
        self.addCleanup(connection.close)
        return connection

    def test_rich_fixture_preserves_provenance_and_is_idempotent(self):
        source = self._source(line_count=9, analysis_count=11, job_count=2)
        target = sqlite3.connect(":memory:")
        target.row_factory = sqlite3.Row
        create_sqlite_schema(target)
        self.addCleanup(target.close)

        first = migrate_legacy(source, target)
        self.assertEqual(first["status"], "PASSED")
        self.assertTrue(first["authoritative_semantic_match"])
        self.assertEqual(target.execute(
            "SELECT COUNT(*) FROM coverage_legacy_provenance"
        ).fetchone()[0], 9 + 11 + 1 + 2)
        provenance_hashes = [
            row[0] for row in target.execute(
                "SELECT provenance_key_hash FROM coverage_legacy_provenance"
            ).fetchall()
        ]
        self.assertTrue(all(len(value) == 64 for value in provenance_hashes))
        self.assertEqual(len(provenance_hashes), len(set(provenance_hashes)))
        self.assertEqual(target.execute(
            "SELECT COUNT(*) FROM coverage_schema_migrations"
        ).fetchone()[0], 0)
        second = migrate_legacy(source, target)
        self.assertTrue(second["authoritative_semantic_match"])
        self.assertEqual(target.execute(
            "SELECT COUNT(*) FROM coverage_scans"
        ).fetchone()[0], 1)

    def test_schema_apply_records_stage_ledger_and_rejects_checksum_drift(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        ddl_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "scripts", "upgrade", "vnext_schema.sql",
        )
        first = apply_schema(connection, ddl_path, release_sha="a" * 40)
        self.assertEqual(first["status"], "PASSED")
        self.assertFalse(first["idempotent"])
        row = connection.execute(
            "SELECT schema_key, schema_version, migration_id "
            "FROM coverage_schema_meta WHERE schema_key='coverage_vnext_core'"
        ).fetchone()
        self.assertEqual(tuple(row), ("coverage_vnext_core", 1, "coverage-vnext-core-v2"))
        second = apply_schema(connection, ddl_path, release_sha="b" * 40)
        self.assertTrue(second["idempotent"])

    def test_database_identity_rejects_same_connection_and_allows_separate_sqlite(self):
        source = sqlite3.connect(":memory:")
        target = sqlite3.connect(":memory:")
        self.addCleanup(source.close)
        self.addCleanup(target.close)
        self.assertNotEqual(
            fingerprint_connection(source)["runtime_key"],
            fingerprint_connection(target)["runtime_key"],
        )
        result = assert_separate_connections(source, target)
        self.assertEqual(result["status"], "PASSED")
        with self.assertRaises(ValueError):
            assert_separate_connections(source, source)

    def test_empty_target_gate_accepts_empty_and_bootstrap_only_targets(self):
        empty = sqlite3.connect(":memory:")
        self.addCleanup(empty.close)
        self.assertEqual(
            assert_empty_vnext_target(empty)["result"], "EMPTY"
        )

        bootstrap = sqlite3.connect(":memory:")
        bootstrap.row_factory = sqlite3.Row
        self.addCleanup(bootstrap.close)
        bootstrap.executescript("""
            CREATE TABLE coverage_schema_meta(
                schema_key TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
                applied_at TEXT NOT NULL, release_sha TEXT NOT NULL DEFAULT '',
                migration_id TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE coverage_schema_migrations(
                migration_id TEXT PRIMARY KEY, schema_key TEXT NOT NULL,
                from_version INTEGER NOT NULL, to_version INTEGER NOT NULL,
                ddl_sha256 TEXT NOT NULL, state TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT,
                release_sha TEXT NOT NULL DEFAULT '', error_class TEXT NOT NULL DEFAULT ''
            );
        """)
        result = assert_empty_vnext_target(bootstrap)
        self.assertEqual(result["result"], "BOOTSTRAP_ONLY")

    def test_empty_target_gate_rejects_business_schema_rows_and_unknown_tables(self):
        existing_schema = sqlite3.connect(":memory:")
        self.addCleanup(existing_schema.close)
        existing_schema.execute(
            "CREATE TABLE coverage_projects(id INTEGER PRIMARY KEY, project_name TEXT)"
        )
        with self.assertRaisesRegex(RuntimeError, "business_schema_present"):
            assert_empty_vnext_target(existing_schema)

        unknown = sqlite3.connect(":memory:")
        self.addCleanup(unknown.close)
        unknown.execute("CREATE TABLE operator_scratch(value TEXT)")
        with self.assertRaisesRegex(RuntimeError, "unknown_tables=operator_scratch"):
            assert_empty_vnext_target(unknown)

        populated = sqlite3.connect(":memory:")
        self.addCleanup(populated.close)
        populated.execute(
            "CREATE TABLE coverage_projects(id INTEGER PRIMARY KEY, project_name TEXT)"
        )
        populated.execute("INSERT INTO coverage_projects VALUES(1, 'existing')")
        with self.assertRaisesRegex(RuntimeError, "business_rows=coverage_projects"):
            assert_empty_vnext_target(populated, allow_initialized_schema=True)

    def test_apply_schema_rejects_nonempty_target_before_business_ddl(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE TABLE coverage_projects(id INTEGER PRIMARY KEY, project_name TEXT)"
        )
        statements = []
        connection.set_trace_callback(statements.append)
        ddl_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "scripts", "upgrade", "vnext_schema.sql",
        )
        with self.assertRaisesRegex(RuntimeError, "business_schema_present"):
            apply_schema(connection, ddl_path)
        self.assertFalse(any("CREATE TABLE coverage_schema" in item for item in statements))

    def test_apply_schema_persists_target_preflight_evidence(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        ddl_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "scripts", "upgrade", "vnext_schema.sql",
        )
        result = apply_schema(connection, ddl_path, release_sha="a" * 40)
        self.assertEqual(result["target_preflight"]["result"], "EMPTY")
        row = connection.execute("""
            SELECT target_emptiness_result, target_table_inventory_hash,
                   target_preflight_at, state
            FROM coverage_schema_migrations
            WHERE migration_id='coverage-vnext-core-v2'
        """).fetchone()
        self.assertEqual(row[0], "EMPTY")
        self.assertEqual(len(row[1]), 64)
        self.assertTrue(row[2])
        self.assertEqual(row[3], "APPLIED")


if __name__ == "__main__":
    unittest.main()
