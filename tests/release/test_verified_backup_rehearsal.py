import gzip
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest

from scripts.upgrade.run_verified_backup_rehearsal import (
    _load_expected_sha,
    _load_backup_provenance_manifest,
    _migrated_file_state_ready_evidence,
    _safe_database_name,
    _validate_dump,
)
from scripts.upgrade.legacy_fixture import (
    create_legacy_fixture_schema, seed_legacy_fixture,
)
from scripts.upgrade.migration_runner import create_sqlite_schema, migrate_legacy


class TestVerifiedBackupRehearsal(unittest.TestCase):
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))

    def test_disposable_database_names_are_namespaced(self):
        self.assertEqual(
            _safe_database_name("coverage_gate_a_source_abc", "source"),
            "coverage_gate_a_source_abc",
        )
        with self.assertRaises(ValueError):
            _safe_database_name("coverage", "source")
        with self.assertRaises(ValueError):
            _safe_database_name("coverage_gate_a_source;DROP", "source")

    def test_dump_requires_hash_and_external_storage(self):
        with tempfile.TemporaryDirectory() as root:
            deploy = os.path.join(root, "candidate")
            os.makedirs(deploy)
            dump = os.path.join(root, "verified", "full.sql.gz")
            os.makedirs(os.path.dirname(dump))
            with gzip.open(dump, "wb") as stream:
                stream.write(b"CREATE TABLE coverage_example (id INT);\n")
            with open(dump, "rb") as stream:
                digest = hashlib.sha256(stream.read()).hexdigest()
            resolved, actual, expanded = _validate_dump(
                dump, digest, deploy, [deploy],
            )
            self.assertEqual(resolved, os.path.realpath(dump))
            self.assertEqual(actual, digest)
            self.assertGreater(expanded, 0)

            with self.assertRaises(ValueError):
                _validate_dump(dump, "0" * 64, deploy, [deploy])

    def test_dump_sidecar_hash_is_supported(self):
        with tempfile.TemporaryDirectory() as root:
            dump = os.path.join(root, "full.sql.gz")
            with gzip.open(dump, "wb") as stream:
                stream.write(b"fixture")
            with open(dump, "rb") as stream:
                digest = hashlib.sha256(stream.read()).hexdigest()
            with open(dump + ".sha256", "w") as stream:
                stream.write("{}  full.sql.gz\n".format(digest))
            self.assertEqual(_load_expected_sha(dump, ""), digest)

    def test_backup_provenance_requires_non_synthetic_production_attestation(self):
        with tempfile.TemporaryDirectory() as root:
            dump = os.path.join(root, "full.sql.gz")
            with gzip.open(dump, "wb") as stream:
                stream.write(b"CREATE TABLE coverage_example (id INT);\n")
            with open(dump, "rb") as stream:
                dump_sha = hashlib.sha256(stream.read()).hexdigest()
            with self.assertRaises(ValueError) as missing:
                _load_backup_provenance_manifest(
                    "", dump, dump_sha, self.repo_root, [],
                )
            self.assertIn("--backup-manifest", str(missing.exception))

            manifest_path = os.path.join(root, "backup-manifest.json")
            with open(manifest_path, "w", encoding="utf-8") as stream:
                json.dump({
                    "status": "BACKUP_VERIFIED",
                    "evidence_class": "production_backup",
                    "synthetic": True,
                }, stream)
            with self.assertRaises(ValueError) as synthetic:
                _load_backup_provenance_manifest(
                    manifest_path, dump, dump_sha, self.repo_root, [],
                )
            self.assertIn("synthetic", str(synthetic.exception))

    def test_backup_provenance_binds_dump_and_restore_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            dump = os.path.join(root, "full.sql.gz")
            with gzip.open(dump, "wb") as stream:
                stream.write(b"CREATE TABLE coverage_example (id INT);\n")
            with open(dump, "rb") as stream:
                dump_sha = hashlib.sha256(stream.read()).hexdigest()
            manifest_path = os.path.join(root, "backup-manifest.json")
            manifest = {
                "status": "BACKUP_VERIFIED",
                "evidence_class": "production_backup",
                "synthetic": False,
                "backup_root_external": True,
                "database": "coverage",
                "full_sql_gz_size": os.path.getsize(dump),
                "full_sql_gz_sha256": dump_sha,
                "snapshot": {"tables": {"coverage_analysis": {"count": 1}}},
                "verification": {
                    "table_inventory": ["coverage_example"],
                    "restore_smoke": "PASSED",
                    "restore_target_empty_before_restore": True,
                    "restore_database_runtime_identity": {"version": "11.8"},
                },
                "provenance": {
                    "source_environment": "production",
                    "operator": "release-operator",
                    "attested_at": "2026-08-21T00:00:00Z",
                },
            }
            with open(manifest_path, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream)
            resolved, manifest_sha, loaded = _load_backup_provenance_manifest(
                manifest_path, dump, dump_sha, self.repo_root, [],
            )
            self.assertEqual(resolved, os.path.realpath(manifest_path))
            self.assertEqual(len(manifest_sha), 64)
            self.assertEqual(loaded["provenance"]["source_environment"], "production")

            manifest["full_sql_gz_sha256"] = "0" * 64
            with open(manifest_path, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream)
            with self.assertRaises(ValueError) as mismatch:
                _load_backup_provenance_manifest(
                    manifest_path, dump, dump_sha, self.repo_root, [],
                )
            self.assertIn("SHA256", str(mismatch.exception))

    def test_migrated_file_state_ready_evidence_checks_all_projects(self):
        source = sqlite3.connect(":memory:")
        target = sqlite3.connect(":memory:")
        source.row_factory = sqlite3.Row
        target.row_factory = sqlite3.Row
        self.addCleanup(source.close)
        self.addCleanup(target.close)
        create_legacy_fixture_schema(source)
        seed_legacy_fixture(
            source, project_name="verified-fixture", line_count=8,
            analysis_count=5, draft_stride=2,
        )
        create_sqlite_schema(target)
        result = migrate_legacy(source, target, release_sha="verified-fixture")
        self.assertEqual(result["status"], "PASSED")
        evidence = _migrated_file_state_ready_evidence(target)
        self.assertEqual(evidence["status"], "PASSED")
        self.assertEqual(evidence["project_count"], 1)
        project = evidence["projects"]["verified-fixture"]
        self.assertEqual(project["status"], "PASSED")
        self.assertEqual(project["data_version"], project["file_state_version"])
        self.assertEqual(project["gate"]["pending_conservation"]["status"], "PASSED")


if __name__ == "__main__":
    unittest.main()
