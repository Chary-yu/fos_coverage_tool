"""
Targeted Tests for Phase 0 Baseline & Zero-Data-Loss Safety Gates (Items 18, 19, 20, 21, 24, 25, 26, 28)
"""

import unittest
import os
import sys
import tempfile
import shutil
import json
import gzip

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.release_identity import generate_release_identity, save_release_manifest, load_release_manifest
from app.db.connection_pool import close_global_pool, get_global_pool
from scripts.diagnostics.data_hash_gate import verify_data_integrity
from scripts.maintenance.mysql_backup import (
    perform_database_backup, compute_file_sha256, verify_mysql_backup,
)
from scripts.upgrade.run_upgrade import resolve_backup_root
from scripts.upgrade.schema_preflight import analyze_sql_script
from scripts.diagnostics.path_mapping_audit import (
    PathLookupIndex, audit_path_mappings, normalize_path,
)
from scripts.diagnostics.security_scanner import scan_file
from scripts.upgrade.evidence_manifest import ProductionEvidenceManifest

class TestPhase0Baseline(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_item_18_release_identity(self):
        """Verify unified release identity generation and asset hashing."""
        dummy_js = os.path.join(self.test_dir, "test.js")
        with open(dummy_js, "w") as f:
            f.write("console.log('v1');")
            
        ident = generate_release_identity(
            repo_root=_REPO_ROOT,
            version="v11.4",
            asset_files=[dummy_js]
        )
        self.assertEqual(ident["version"], "v11.4")
        self.assertTrue(len(ident["commit_sha"]) >= 8)
        self.assertTrue(len(ident["asset_hash"]) > 0)
        self.assertEqual(ident["schema_version"], 2)
        
        # Save and reload
        mpath = os.path.join(self.test_dir, "manifest.json")
        save_release_manifest(mpath, ident)
        loaded = load_release_manifest(mpath)
        self.assertEqual(loaded["build_id"], ident["build_id"])

    def test_item_19_data_hash_gate_integrity(self):
        """Verify data hash gate catches row decreases and content mismatches."""
        pre = {
            "tables": {
                "coverage_analysis": {"count": 100, "content_hash": "hash_a"},
                "coverage_line_index": {"count": 500, "content_hash": "hash_b"},
                "coverage_project_state": {"count": 2, "content_hash": "hash_c", "versions": {"p1": 1, "p2": 3}},
                "coverage_background_jobs": {"count": 5, "content_hash": "hash_d", "status_distribution": {"completed": 5}}
            }
        }
        
        # Matching post
        valid, errors = verify_data_integrity(pre, pre)
        self.assertTrue(valid)
        self.assertEqual(len(errors), 0)
        
        # Corrupted post (count decrease)
        post_corrupted = {
            "tables": {
                "coverage_analysis": {"count": 99, "content_hash": "hash_diff"},
                "coverage_line_index": {"count": 500, "content_hash": "hash_b"},
                "coverage_project_state": {"count": 2, "content_hash": "hash_c", "versions": {"p1": 1}},
                "coverage_background_jobs": {"count": 5, "content_hash": "hash_d", "status_distribution": {"completed": 5}}
            }
        }
        valid2, errors2 = verify_data_integrity(pre, post_corrupted)
        self.assertFalse(valid2)
        self.assertTrue(any("Row count decreased" in e for e in errors2))
        self.assertTrue(any("Content hash mismatch" in e for e in errors2))
        self.assertTrue(any("Project state disappeared" in e for e in errors2))

    def test_item_20_mysql_backup_manifest(self):
        """Verify backup generation creates valid sha256 and manifest."""
        backup_dir = os.path.join(self.test_dir, "backup")
        ok, manifest, err = perform_database_backup(
            db_config={"database": "test_db"},
            backup_dir=backup_dir,
            allow_mock_in_test=True
        )
        self.assertTrue(ok)
        self.assertTrue(os.path.isfile(os.path.join(backup_dir, "full.sql.gz")))
        self.assertTrue(os.path.isfile(os.path.join(backup_dir, "full.sql.gz.sha256")))
        self.assertEqual(manifest["status"], "BACKUP_VERIFIED")
        self.assertTrue(manifest["synthetic"])
        self.assertIn("provenance", manifest)
        self.assertEqual(manifest["provenance"]["operator"], "")

    def test_backup_root_must_not_be_inside_deployment_root(self):
        deployment_root = os.path.join(self.test_dir, "candidate")
        backup_dir = os.path.join(deployment_root, "backup")
        ok, manifest, err = perform_database_backup(
            db_config={
                "database": "test_db",
                "deployment_roots": [deployment_root],
            },
            backup_dir=backup_dir,
            allow_mock_in_test=True,
        )
        self.assertFalse(ok)
        self.assertEqual(manifest, {})
        self.assertIn("outside", err)
        self.assertFalse(os.path.exists(backup_dir))

    def test_backup_restore_rejects_source_target_alias(self):
        dump_path = os.path.join(self.test_dir, "full.sql.gz")
        schema_path = os.path.join(self.test_dir, "schema.sql")
        with gzip.open(dump_path, "wb") as stream:
            stream.write(b"-- fixture dump\n")
        with open(schema_path, "w") as stream:
            stream.write("CREATE TABLE `coverage_example` (id INT);\n")
        ok, result, err = verify_mysql_backup(
            dump_path,
            schema_path,
            expected_sha256=compute_file_sha256(dump_path),
            db_config={"database": "same_database"},
            restore_database="same_database",
        )
        self.assertFalse(ok)
        self.assertEqual(result["restore_smoke"], "NOT_REQUESTED")
        self.assertIn("differ", err)

    def test_backup_root_resolution_rejects_deployment_child(self):
        with self.assertRaises(ValueError):
            resolve_backup_root(self.test_dir, os.path.join(self.test_dir, "backup"))
        resolved = resolve_backup_root(self.test_dir, "../outside-backups")
        self.assertFalse(os.path.commonpath([
            os.path.realpath(resolved), os.path.realpath(self.test_dir),
        ]) == os.path.realpath(self.test_dir))

    def test_item_21_schema_preflight_rules(self):
        """Verify schema preflight blocks destructive DDL while allowing additive migrations."""
        destructive_sql = "DROP TABLE coverage_analysis; TRUNCATE TABLE coverage_line_index;"
        safe, errs, warns = analyze_sql_script(destructive_sql)
        self.assertFalse(safe)
        self.assertTrue(any("DROP TABLE" in e for e in errs))
        self.assertTrue(any("TRUNCATE" in e for e in errs))
        
        additive_sql = """
        CREATE TABLE IF NOT EXISTS coverage_file_state (
            project_name VARCHAR(128) NOT NULL,
            file_path_hash VARCHAR(64) NOT NULL,
            PRIMARY KEY (project_name, file_path_hash)
        );
        CREATE INDEX idx_file_state ON coverage_file_state (project_name);
        """
        safe2, errs2, warns2 = analyze_sql_script(additive_sql)
        self.assertTrue(safe2)
        self.assertEqual(len(errs2), 0)

    def test_item_25_path_mapping_safety(self):
        """Verify path mapping rules: normalized, suffix, ambiguous fail-closed, basename reject."""
        targets = ["src/module_a/foo.c", "src/module_b/foo.c", "src/unique/bar.c"]
        idx = PathLookupIndex(targets)
        
        # Exact
        p, cls = idx.resolve("src/module_a/foo.c")
        self.assertEqual(cls, "exact")
        
        # Unique suffix
        p2, cls2 = idx.resolve("unique/bar.c")
        self.assertEqual(cls2, "unique_suffix")
        self.assertEqual(p2, "src/unique/bar.c")
        
        # Ambiguous suffix -> fail-closed
        p3, cls3 = idx.resolve("foo.c")
        self.assertIn(cls3, ["ambiguous_suffix", "basename_only_rejected"])
        self.assertIsNone(p3)

        # External LCOV identities with parent traversal are invalid, not a
        # normalized alias for a different source file.
        with self.assertRaises(ValueError):
            normalize_path("../src/module_a/foo.c")
        self.assertEqual(
            idx.resolve("../src/module_a/foo.c"),
            (None, "invalid_path"),
        )

    def test_connection_pool_isolated_by_database_identity(self):
        first = get_global_pool({"host": "db", "port": 3306,
                                 "user": "coverage", "database": "coverage"})
        same = get_global_pool({"host": "db", "port": 3306,
                                "user": "coverage", "database": "coverage"})
        candidate = get_global_pool({"host": "db", "port": 3306,
                                     "user": "coverage", "database": "coverage_candidate"})
        try:
            self.assertIs(first, same)
            self.assertIsNot(first, candidate)
        finally:
            close_global_pool()

    def test_item_28_evidence_manifest_governance(self):
        """Verify evidence manifest structure and final gate validation."""
        pem = ProductionEvidenceManifest(repo_root=self.test_dir)
        pem.record("release_identity", {"version": "v11.7", "commit_sha": "abc1234", "build_id": "v11.7-abc1234-12345678"})
        pem.record("schema_migration", {"preflight_safe": True})
        pem.record("data_hash_verification", {"verified": True, "evidence_class": "production_database"})
        pem.record("targeted_tests", {"phase0": {"status": "PASSED"}})
        pem.record("browser_smoke_suite", {"status": "PASSED", "evidence_class": "real_browser"})
        pem.record("sidecar_audit", {"is_safe": True})
        pem.record("security_audit", {"is_safe": True, "critical_count": 0, "high_count": 0})
        pem.record("performance_benchmark", {
            "Tier_A_1k": {"status": "PASSED"},
            "Tier_B_10k": {"status": "PASSED"},
            "Tier_C_50k": {"status": "PASSED"},
            "Tier_D_100k": {"status": "PASSED"}
        })
        pem.record("backup_evidence", {"status": "BACKUP_VERIFIED", "evidence_class": "production_backup", "full_sql_gz_sha256": "fakehash"})
        
        passed, unmet = pem.validate_final_gate()
        # A synthetic ledger from a temp directory has no exact revision and
        # must not certify production, even when its booleans look green.
        self.assertFalse(passed)
        self.assertIn("Evidence revision does not match current commit", unmet)
        self.assertEqual(pem.data["status"], "UNMET_GATES")

if __name__ == "__main__":
    unittest.main()
