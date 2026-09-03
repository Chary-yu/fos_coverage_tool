import sqlite3
import unittest

from scripts.maintenance.mysql_backup import _capture_generation_aware_snapshot
from scripts.upgrade.migration_runner import create_sqlite_schema


class VNextBackupSnapshotTest(unittest.TestCase):
    def test_backup_snapshot_does_not_call_legacy_four_table_model(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        create_sqlite_schema(connection)
        connection.execute(
            "INSERT INTO coverage_projects(project_name, created_at, updated_at) "
            "VALUES (?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            ("FOS_V6R2",),
        )
        connection.commit()
        snapshot = _capture_generation_aware_snapshot(connection)
        self.assertEqual(snapshot["generation"], "VNEXT")
        self.assertIn("coverage_projects", snapshot["tables"])
        self.assertNotIn("coverage_analysis", snapshot["tables"])
        self.assertEqual(snapshot["tables"]["coverage_projects"]["count"], 1)
        self.assertRegex(snapshot["semantic_hash"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
