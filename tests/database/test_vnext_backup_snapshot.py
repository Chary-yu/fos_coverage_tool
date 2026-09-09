import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, time
from decimal import Decimal
from unittest import mock

from scripts.maintenance.mysql_backup import (
    _atomic_write_json,
    _capture_generation_aware_snapshot,
    _json_safe,
    perform_database_backup,
)
from scripts.upgrade.evidence_manifest import ProductionEvidenceManifest
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

    def test_json_safe_normalizes_database_driver_types_recursively(self):
        payload = {
            "datetime": datetime(2026, 9, 9, 11, 32, 57),
            "date": date(2026, 9, 9),
            "time": time(11, 32, 57),
            "decimal": Decimal("12.3400"),
            "bytes": b"\x00\xff",
            "nested": [{"tuple": (datetime(2026, 1, 2, 3, 4, 5),)}],
        }
        normalized = _json_safe(payload)
        self.assertEqual(normalized["datetime"], "2026-09-09 11:32:57")
        self.assertEqual(normalized["date"], "2026-09-09")
        self.assertEqual(normalized["time"], "11:32:57")
        self.assertEqual(normalized["decimal"], "12.3400")
        self.assertEqual(normalized["bytes"], "00ff")
        self.assertEqual(
            normalized["nested"][0]["tuple"], ["2026-01-02 03:04:05"]
        )
        json.dumps(normalized, sort_keys=True)

    def test_json_safe_rejects_unknown_types_fail_closed(self):
        class Unsupported(object):
            pass

        with self.assertRaises(TypeError):
            _json_safe({"value": Unsupported()})

    def test_atomic_json_write_does_not_leave_partial_manifest(self):
        root = tempfile.TemporaryDirectory(prefix="backup-json-")
        self.addCleanup(root.cleanup)
        path = os.path.join(root.name, "backup-manifest.json")

        class Unsupported(object):
            pass

        with self.assertRaises(TypeError):
            _atomic_write_json(path, {"bad": Unsupported()})
        self.assertFalse(os.path.exists(path))
        leftovers = [
            name for name in os.listdir(root.name)
            if name.startswith(".backup-manifest.json-tmp-")
        ]
        self.assertEqual(leftovers, [])

    def test_perform_backup_returns_json_native_manifest_and_records_release_evidence(self):
        root = tempfile.TemporaryDirectory(prefix="backup-manifest-")
        self.addCleanup(root.cleanup)
        snapshot = {
            "snapshot_version": "existing-vnext-authoritative-facts-v3",
            "captured_at": "2026-09-09T11:32:57Z",
            "generation": "VNEXT",
            "semantic_hash": "a" * 64,
            "components": {
                "projects": [{
                    "created_at": datetime(2026, 9, 9, 11, 32, 57),
                    "budget": Decimal("7.50"),
                    "marker": b"\x01\x02",
                }],
            },
            "counts": {"projects": 1},
            "component_hashes": {"projects": "b" * 64},
            "tables": {
                "coverage_projects": {
                    "count": 1,
                    "content_hash": "b" * 64,
                }
            },
        }
        config = {"database": "coverage_test"}
        with mock.patch(
            "scripts.maintenance.mysql_backup._capture_generation_aware_snapshot",
            return_value=snapshot,
        ), mock.patch(
            "scripts.maintenance.mysql_backup.subprocess.run"
        ) as run_mock, mock.patch(
            "scripts.maintenance.mysql_backup.subprocess.Popen"
        ) as popen_mock:
            run_mock.side_effect = [
                mock.Mock(returncode=1, stdout=b"", stderr=b""),
            ]
            popen_mock.side_effect = AssertionError(
                "mock backup path must not invoke mysqldump"
            )
            ok, manifest, error = perform_database_backup(
                config,
                root.name,
                connection=object(),
                allow_mock_in_test=True,
            )
        self.assertTrue(ok, error)
        json.dumps(manifest, sort_keys=True)
        disk_path = os.path.join(root.name, "backup-manifest.json")
        with open(disk_path, encoding="utf-8") as stream:
            disk_manifest = json.load(stream)
        self.assertEqual(disk_manifest, manifest)
        self.assertEqual(
            manifest["snapshot"]["components"]["projects"][0]["created_at"],
            "2026-09-09 11:32:57",
        )
        self.assertEqual(
            manifest["snapshot"]["components"]["projects"][0]["budget"],
            "7.50",
        )
        self.assertEqual(
            manifest["snapshot"]["components"]["projects"][0]["marker"],
            "0102",
        )

        evidence_root = tempfile.TemporaryDirectory(prefix="backup-evidence-")
        self.addCleanup(evidence_root.cleanup)
        old_evidence = os.environ.get("COVERAGE_EVIDENCE_DIR")
        os.environ["COVERAGE_EVIDENCE_DIR"] = evidence_root.name
        self.addCleanup(
            lambda: (
                os.environ.__setitem__("COVERAGE_EVIDENCE_DIR", old_evidence)
                if old_evidence is not None
                else os.environ.pop("COVERAGE_EVIDENCE_DIR", None)
            )
        )
        ledger = ProductionEvidenceManifest(root.name)
        manifest["revision"] = "a" * 40
        manifest["command"] = "mysqldump --single-transaction --quick coverage_test"
        manifest["exit_code"] = 0
        manifest["artifact_path"] = os.path.join(root.name, "full.sql.gz")
        ledger.record("backup_evidence", manifest)
        with open(ledger.manifest_path, encoding="utf-8") as stream:
            persisted = json.load(stream)
        self.assertEqual(
            persisted["backup_evidence"]["snapshot"]["components"]["projects"][0]["created_at"],
            "2026-09-09 11:32:57",
        )


if __name__ == "__main__":
    unittest.main()
