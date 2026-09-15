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
    verify_candidate_only_workflow_jobs,
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
        self.assertNotIn(
            'run.get("conclusion") != "success"', collector
        )
        self.assertIn("verify_candidate_only_workflow_jobs", collector)
        self.assertIn("Require all Production READY evidence lanes to pass", collector)
        self.assertIn("Build fail-closed Gate A-F evidence matrix", collector)
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

    @staticmethod
    def _job(name, conclusion="success", steps=None):
        job = {
            "id": abs(hash(name)) % 100000 + 1,
            "name": name,
            "status": "completed",
            "conclusion": conclusion,
        }
        if steps is not None:
            job["steps"] = steps
        return job

    @staticmethod
    def _step(name, conclusion="success"):
        return {
            "name": name,
            "status": "completed",
            "conclusion": conclusion,
        }

    def _candidate_only_jobs(self):
        ready_steps = [
            self._step("Checkout Code"),
            self._step(
                "Require all Production READY evidence lanes to pass",
                "failure",
            ),
            self._step("Upload Production READY identity join"),
        ]
        jobs = [
            self._job("Candidate source identity gate"),
            self._job("Candidate source gate (required source lanes)"),
            self._job(PRODUCTION_CANDIDATE_CALLER_JOB_NAME),
            self._job(
                "Trusted Validation Candidate Build (manual protected lane)",
                "skipped",
            ),
            self._job(
                "Verified production backup rehearsal (MariaDB 5.5)",
                "skipped",
            ),
            self._job("Real Candidate browser evidence", "skipped"),
            self._job(
                "Cross-layer performance release evidence (browser artifact)",
                "skipped",
            ),
            self._job(
                "Production READY gate (manual external evidence)",
                "failure",
                ready_steps,
            ),
        ]
        for version in ("3.10", "3.12"):
            jobs.append(self._job(
                "Test Suite (Python {})".format(version),
                "success",
                [self._step("Run ordinary regression steps")],
            ))
        return jobs

    def test_candidate_only_allows_expected_external_evidence_failure(self):
        result = verify_candidate_only_workflow_jobs(
            self._candidate_only_jobs(), "failure"
        )
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["mode"], "candidate_only")
        self.assertTrue(result["production_ready_failure_allowed"])
        self.assertEqual(
            result["external_evidence_job_conclusions"][
                "Real Candidate browser evidence"
            ],
            "skipped",
        )

    def test_candidate_only_blocks_production_candidate_failure(self):
        jobs = self._candidate_only_jobs()
        for job in jobs:
            if job["name"] == PRODUCTION_CANDIDATE_CALLER_JOB_NAME:
                job["conclusion"] = "failure"
        with self.assertRaisesRegex(ValueError, "Production Candidate job did not pass"):
            verify_candidate_only_workflow_jobs(jobs, "failure")

    def test_candidate_only_blocks_source_gate_failure(self):
        jobs = self._candidate_only_jobs()
        for job in jobs:
            if job["name"] == "Candidate source gate (required source lanes)":
                job["conclusion"] = "failure"
        with self.assertRaisesRegex(ValueError, "expected success conclusion"):
            verify_candidate_only_workflow_jobs(jobs, "failure")

    def test_candidate_only_blocks_unrelated_regression_failure(self):
        jobs = self._candidate_only_jobs()
        jobs.append(self._job("R8 release orchestration regression", "failure"))
        with self.assertRaisesRegex(ValueError, "unexpected Candidate-only workflow job failure"):
            verify_candidate_only_workflow_jobs(jobs, "failure")

    def test_candidate_only_allows_exact_gate_a_f_fail_closed_step(self):
        jobs = self._candidate_only_jobs()
        for job in jobs:
            if job["name"] in ("Test Suite (Python 3.10)", "Test Suite (Python 3.12)"):
                job["conclusion"] = "failure"
                job["steps"] = [
                    self._step("Run ordinary regression steps"),
                    self._step(
                        "Build fail-closed Gate A-F evidence matrix", "failure"
                    ),
                    self._step("Build per-task Gate A-F status", "skipped"),
                ]
        result = verify_candidate_only_workflow_jobs(jobs, "failure")
        self.assertTrue(result["candidate_only_failure_allowed"])
        self.assertTrue(
            result["test_suite_results"]["Test Suite (Python 3.10)"][
                "gate_a_f_failure_allowed"
            ]
        )

    def test_candidate_only_blocks_failure_before_gate_a_f_step(self):
        jobs = self._candidate_only_jobs()
        job = next(
            item for item in jobs if item["name"] == "Test Suite (Python 3.10)"
        )
        job["conclusion"] = "failure"
        job["steps"] = [
            self._step("Run ordinary regression steps", "failure"),
            self._step(
                "Build fail-closed Gate A-F evidence matrix", "failure"
            ),
        ]
        with self.assertRaisesRegex(ValueError, "failed outside the expected fail-closed step"):
            verify_candidate_only_workflow_jobs(jobs, "failure")

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
        artifact_role = "production_candidate"
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
