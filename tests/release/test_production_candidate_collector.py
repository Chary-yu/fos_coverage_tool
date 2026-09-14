import glob
import json
import os
import tempfile
import unittest

from scripts.release.production_candidate_evidence import (
    PRODUCTION_CANDIDATE_CALLED_JOB_NAME,
    PRODUCTION_CANDIDATE_CALLER_JOB_NAME,
    compare_application_inventory,
    sha256_file,
    select_unique_production_candidate_job,
    verify_protected_receipt_evidence,
)


class ProductionCandidateCollectorTest(unittest.TestCase):
    def test_run_instance_api_and_operator_secret_boundary(self):
        paths = sorted(glob.glob(
            "fos_r8_production_candidate_collect_*_20260914.txt"
        ))
        if not paths:
            self.skipTest("generated collector TXT is not present in this checkout")
        with open(paths[-1], encoding="utf-8") as stream:
            collector = stream.read()
        self.assertIn(
            '"repos/$REPOSITORY/actions/runs/$RUN_ID"', collector
        )
        self.assertNotIn(
            'actions/workflows/ci.yml/runs/$RUN_ID', collector
        )
        self.assertNotIn("COVERAGE_BUILD_PROVENANCE_HMAC_KEY", collector)
        self.assertIn("protected_receipt_verification.json", collector)
        self.assertIn("protected_receipt_verification_attestation.bundle.json", collector)

    def test_reusable_job_accepts_exact_caller_and_called_names_only(self):
        for name in (
                PRODUCTION_CANDIDATE_CALLER_JOB_NAME,
                PRODUCTION_CANDIDATE_CALLED_JOB_NAME):
            job = select_unique_production_candidate_job([{
                "id": 17,
                "name": name,
                "status": "completed",
                "conclusion": "success",
            }])
            self.assertEqual(job["name"], name)
        with self.assertRaisesRegex(ValueError, "missing or ambiguous"):
            select_unique_production_candidate_job([{
                "name": "Trusted Production Candidate Build (manual protected lane) / extra",
                "status": "completed",
                "conclusion": "success",
            }])
        with self.assertRaisesRegex(ValueError, "missing or ambiguous"):
            select_unique_production_candidate_job([
                {"name": PRODUCTION_CANDIDATE_CALLER_JOB_NAME,
                 "status": "completed", "conclusion": "success"},
                {"name": PRODUCTION_CANDIDATE_CALLED_JOB_NAME,
                 "status": "completed", "conclusion": "success"},
            ])
        with self.assertRaisesRegex(ValueError, "did not pass"):
            select_unique_production_candidate_job([{
                "name": PRODUCTION_CANDIDATE_CALLED_JOB_NAME,
                "status": "completed",
                "conclusion": "failure",
            }])

    def test_application_inventory_uses_app_manifest_namespace(self):
        source = [
            {"path": "enhance_coverage.py", "size": 3, "sha256": "a" * 64},
            {"path": "scripts/compat/git", "size": 4, "sha256": "b" * 64},
        ]
        candidate = list(source)
        manifest = [
            {"path": "reports/report.html", "size": 1, "sha256": "c" * 64},
            {"path": "app/enhance_coverage.py", "size": 3, "sha256": "a" * 64},
            {"path": "app/scripts/compat/git", "size": 4, "sha256": "b" * 64},
        ]
        normalized = compare_application_inventory(source, candidate, manifest)
        self.assertEqual(
            [item["path"] for item in normalized],
            ["app/enhance_coverage.py", "app/scripts/compat/git"],
        )
        with self.assertRaisesRegex(ValueError, "Candidate manifest"):
            compare_application_inventory(
                source, candidate,
                [{"path": "enhance_coverage.py", "size": 3,
                  "sha256": "a" * 64}],
            )

    @staticmethod
    def _write(path, value, binary=False):
        if binary:
            with open(path, "wb") as stream:
                stream.write(value)
        else:
            with open(path, "w", encoding="utf-8") as stream:
                json.dump(value, stream, sort_keys=True)
                stream.write("\n")

    def test_protected_evidence_verifies_without_local_hmac_secret(self):
        source_sha = "a" * 40
        source_tree_sha = "b" * 40
        builder_sha = "c" * 40
        previous_release_sha = "d" * 40
        served_root_tree_sha256 = "e" * 64
        served_root_identity_sha256 = "f" * 64
        run_id = "12345"
        run_attempt = "2"
        builder_identity = "github-actions/trusted-production-candidate-builder"
        project_name = "FOS_V6R2"
        artifact_role = "production_release"
        with tempfile.TemporaryDirectory(prefix="candidate-collector-") as root:
            candidate_root = os.path.join(root, "candidate")
            os.makedirs(candidate_root)
            identity_path = os.path.join(root, "release_identity.json")
            manifest_path = os.path.join(candidate_root, "candidate_artifact_manifest.json")
            receipt_path = os.path.join(candidate_root, "candidate_build_receipt.json")
            candidate_bundle_path = os.path.join(root, "candidate_attestation.bundle.json")
            evidence_path = os.path.join(root, "protected_receipt_verification.json")
            identity = {"commit_sha": source_sha}
            provenance = {
                "source_commit_sha": source_sha,
                "source_tree_sha": source_tree_sha,
                "build_workflow_identity": builder_identity,
                "build_workflow_sha": builder_sha,
                "build_workflow_run_id": run_id,
                "build_workflow_run_attempt": run_attempt,
                "previous_release_commit_sha": previous_release_sha,
                "served_root_tree_sha256": served_root_tree_sha256,
                "served_root_identity_sha256": served_root_identity_sha256,
            }
            manifest = {
                "commit_sha": source_sha,
                "artifact_sha256": "1" * 64,
                "artifact_role": artifact_role,
                "production_publishable": True,
                "project_name": project_name,
                "source_provenance": provenance,
            }
            receipt = {"payload": {"source_commit_sha": source_sha}, "signature": "signed"}
            self._write(identity_path, identity)
            self._write(manifest_path, manifest)
            self._write(receipt_path, receipt)
            self._write(candidate_bundle_path, {"bundle": "candidate"})
            evidence = {
                "evidence_schema_version": 1,
                "evidence_type": "protected_candidate_receipt_verification",
                "status": "PASSED",
                "hmac_receipt_verification": "PASSED",
                "hmac_verified": True,
                "candidate_commit_sha": source_sha,
                "candidate_artifact_sha256": manifest["artifact_sha256"],
                "candidate_manifest_sha256": sha256_file(manifest_path),
                "candidate_receipt_sha256": sha256_file(receipt_path),
                "candidate_attestation_bundle_sha256": sha256_file(candidate_bundle_path),
                "release_identity_sha256": sha256_file(identity_path),
                "source_commit_sha": source_sha,
                "source_tree_sha": source_tree_sha,
                "build_workflow_identity": builder_identity,
                "build_workflow_sha": builder_sha,
                "build_workflow_run_id": run_id,
                "build_workflow_run_attempt": run_attempt,
                "artifact_role": artifact_role,
                "production_publishable": True,
                "project_name": project_name,
                "application_validation": "PASSED",
                "application_sha256": "2" * 64,
                "application_file_count": 3,
                "previous_release_commit_sha": previous_release_sha,
                "served_root_tree_sha256": served_root_tree_sha256,
                "served_root_identity_sha256": served_root_identity_sha256,
                "verification_runner_policy": "controlled-production-builder",
            }
            self._write(evidence_path, evidence)
            previous_key = os.environ.pop(
                "COVERAGE_BUILD_PROVENANCE_HMAC_KEY", None
            )
            try:
                verified = verify_protected_receipt_evidence(
                    evidence_path, candidate_root, identity_path, manifest_path,
                    receipt_path, candidate_bundle_path, source_sha,
                    source_tree_sha, builder_sha, builder_identity, run_id,
                    run_attempt, project_name, artifact_role,
                    application_sha256="2" * 64,
                    application_file_count=3,
                    previous_release_sha=previous_release_sha,
                    served_root_tree_sha256=served_root_tree_sha256,
                    served_root_identity_sha256=served_root_identity_sha256,
                )
            finally:
                if previous_key is not None:
                    os.environ["COVERAGE_BUILD_PROVENANCE_HMAC_KEY"] = previous_key
            self.assertTrue(verified["hmac_verified"])
            self.assertRegex(sha256_file(evidence_path), r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
