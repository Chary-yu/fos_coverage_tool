import hashlib
import json
import os
import subprocess
import tempfile
import unittest

from app.candidate_artifact import (
    CandidateArtifactManifest, OFFLINE_OPERATOR_PROVENANCE_CLASS,
    PRODUCTION_PROJECT_NAME, PRODUCTION_RELEASE_ARTIFACT_ROLE,
    RELEASE_TRUST_MODE_OFFLINE_OPERATOR, verify_offline_operator_trust,
    build_git_source_provenance,
)


class OfflineOperatorTrustTest(unittest.TestCase):
    def _fixture(self):
        root = tempfile.TemporaryDirectory(prefix="offline-operator-trust-")
        self.addCleanup(root.cleanup)
        source = os.path.join(root.name, "source")
        candidate = os.path.join(root.name, "candidate")
        os.makedirs(source)
        os.makedirs(candidate)
        with open(os.path.join(source, "source.txt"), "w") as stream:
            stream.write("exact source\n")
        subprocess.check_call(["git", "init", "-q"], cwd=source)
        subprocess.check_call(
            ["git", "config", "user.email", "operator@example.invalid"], cwd=source
        )
        subprocess.check_call(
            ["git", "config", "user.name", "Offline Operator"], cwd=source
        )
        subprocess.check_call(["git", "add", "source.txt"], cwd=source)
        subprocess.check_call(["git", "commit", "-q", "-m", "candidate"], cwd=source)
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source
        ).decode().strip()
        tree = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=source
        ).decode().strip()
        for directory in ("reports", "assets", "registry"):
            os.makedirs(os.path.join(candidate, directory))
        with open(os.path.join(candidate, "reports", "index.html"), "w") as stream:
            stream.write("candidate report")
        with open(os.path.join(candidate, "assets", "app.js"), "w") as stream:
            stream.write("candidate asset")
        with open(os.path.join(candidate, "registry", "report.json"), "w") as stream:
            json.dump({"report_id": "report"}, stream)
        identity = {"commit_sha": commit, "build_id": "offline-candidate"}
        provenance = build_git_source_provenance(
            source, identity, "offline-operator",
            build_workflow_run_id="operator-session",
            build_workflow_run_attempt="operator-attempt",
            build_workflow_sha=commit,
            provenance_class=OFFLINE_OPERATOR_PROVENANCE_CLASS,
        )
        provenance.update({
            "previous_release_commit_sha": "1" * 40,
            "served_root_tree_sha256": "2" * 64,
            "served_root_identity_sha256": "3" * 64,
        })
        manifest = CandidateArtifactManifest.build(
            candidate, identity, source_provenance=provenance,
            artifact_role=PRODUCTION_RELEASE_ARTIFACT_ROLE,
            production_publishable=True,
            project_name=PRODUCTION_PROJECT_NAME,
        )
        bundle = os.path.join(root.name, "source.bundle")
        with open(bundle, "wb") as stream:
            stream.write(b"operator source bundle")
        evidence = {
            "schema_version": 1,
            "release_trust_mode": RELEASE_TRUST_MODE_OFFLINE_OPERATOR,
            "trust_class": "OFFLINE_OPERATOR",
            "repository": "Chary-yu/fos_coverage_tool",
            "commit_sha": commit,
            "tree_sha": tree,
            "source_bundle_path": bundle,
            "source_bundle_sha256": hashlib.sha256(
                b"operator source bundle"
            ).hexdigest(),
            "candidate_tree_sha256": manifest["artifact_sha256"],
            "production_host": "coverage-prod-01",
            "production_baseline_sha": "4" * 40,
            "build_timestamp": "2026-09-03T12:00:00Z",
            "validation_session_id": "offline-session-1",
            "protected_builder": "SKIPPED_BY_OPERATOR",
            "offline_operator_source_integrity": "PASSED",
        }
        return root, source, candidate, identity, manifest, evidence

    def test_exact_hashes_pass_and_never_claim_protected_builder(self):
        _root, source, candidate, identity, manifest, evidence = self._fixture()
        result = verify_offline_operator_trust(
            candidate, identity, manifest, source, evidence=evidence,
            source_bundle_path=evidence["source_bundle_path"],
            expected_repository="Chary-yu/fos_coverage_tool",
            expected_production_host="coverage-prod-01",
            expected_production_baseline_sha="4" * 40,
            expected_validation_session_id="offline-session-1",
        )
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["protected_builder"], "SKIPPED_BY_OPERATOR")
        self.assertEqual(result["offline_operator_source_integrity"], "PASSED")
        self.assertEqual(result["trust_class"], "OFFLINE_OPERATOR")
        self.assertEqual(result["source_commit_sha"], identity["commit_sha"])

    def test_source_or_candidate_hash_mismatch_fails_closed(self):
        _root, source, candidate, identity, manifest, evidence = self._fixture()
        bad_source = dict(evidence, tree_sha="f" * 40)
        with self.assertRaisesRegex(ValueError, "tree_sha"):
            verify_offline_operator_trust(
                candidate, identity, manifest, source, evidence=bad_source
            )
        bad_candidate = dict(evidence, candidate_tree_sha256="e" * 64)
        with self.assertRaisesRegex(ValueError, "candidate content SHA256"):
            verify_offline_operator_trust(
                candidate, identity, manifest, source, evidence=bad_candidate
            )

    def test_offline_evidence_marked_protected_is_rejected(self):
        _root, source, candidate, identity, manifest, evidence = self._fixture()
        bad = dict(evidence, release_trust_mode="protected_builder")
        with self.assertRaisesRegex(ValueError, "marked as protected_builder"):
            verify_offline_operator_trust(
                candidate, identity, manifest, source, evidence=bad
            )

    def test_source_bundle_path_is_bound_when_policy_supplies_one(self):
        root, source, candidate, identity, manifest, evidence = self._fixture()
        other_bundle = os.path.join(root.name, "other-source.bundle")
        with open(other_bundle, "wb") as stream:
            stream.write(b"operator source bundle")
        with self.assertRaisesRegex(ValueError, "bundle path does not match policy"):
            verify_offline_operator_trust(
                candidate, identity, manifest, source, evidence=evidence,
                source_bundle_path=other_bundle,
            )


if __name__ == "__main__":
    unittest.main()
