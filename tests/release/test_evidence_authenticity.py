import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.release_identity import get_current_release_identity
from scripts.upgrade.evidence_manifest import EvidenceManifestV2, ProductionEvidenceManifest


class TestEvidenceAuthenticity(unittest.TestCase):
    def test_missing_runtime_release_manifest_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(RuntimeError):
                get_current_release_identity(root)

    def test_mock_backup_and_browser_cannot_pass_final_gate(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = ProductionEvidenceManifest(root)
            manifest.record("release_identity", {
                "version": "v", "commit_sha": "fake", "build_id": "b"
            })
            manifest.record("targeted_tests", {"unit": {"status": "PASSED", "revision": "fake"}})
            manifest.record("browser_smoke_suite", {
                "status": "PASSED", "evidence_class": "mock_dom", "revision": "fake"
            })
            manifest.record("data_hash_verification", {
                "verified": True, "evidence_class": "production_database", "revision": "fake"
            })
            manifest.record("schema_migration", {"preflight_safe": True, "status": "PASSED", "revision": "fake"})
            manifest.record("sidecar_audit", {"is_safe": True, "status": "PASSED", "revision": "fake"})
            manifest.record("security_audit", {"is_safe": True, "status": "PASSED", "revision": "fake", "auth_mode": "reverse_proxy"})
            manifest.record("performance_benchmark", {"Tier_A_1k": {"status": "PASSED", "revision": "fake"}})
            manifest.record("backup_evidence", {
                "status": "BACKUP_VERIFIED", "evidence_class": "mock", "revision": "fake",
                "full_sql_gz_sha256": "placeholder"
            })
            passed, unmet = manifest.validate_final_gate()
            self.assertFalse(passed)
            self.assertTrue(any("Browser" in item or "Backup" in item for item in unmet))

    def test_evidence_manifest_v2_binds_record_to_revision_and_artifact_sha(self):
        with tempfile.TemporaryDirectory() as root:
            artifact = os.path.join(root, "evidence.json")
            with open(artifact, "w") as stream:
                stream.write("{}")
            manifest = EvidenceManifestV2(root, "gate-a", candidate_revision="abc")
            record = manifest.record(
                "schema", "db-integration", "PASSED", "unit-test", 0,
                artifact_path=artifact,
            )
            self.assertEqual(record["candidate_revision"], "abc")
            self.assertTrue(record["artifact_sha256"])
            self.assertEqual(manifest.validate(), (True, []))
            self.assertTrue(manifest.data["manifest_sha256"])


if __name__ == "__main__":
    unittest.main()
