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
    apply_schema, create_sqlite_schema, migrate_legacy,
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


if __name__ == "__main__":
    unittest.main()
