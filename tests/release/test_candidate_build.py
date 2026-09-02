import json
import os
import subprocess
import tempfile
import unittest

from app.candidate_artifact import (
    CandidateArtifactManifest, VALIDATION_FIXTURE_ARTIFACT_ROLE,
)
from app.release_publication import (
    build_release_manifest, normalize_candidate_artifact,
    validate_release_manifest,
)
from app.code_detail.source_reader import calc_sidecar_file_key
from scripts.release.build_candidate_artifact import (
    CANDIDATE_FILE_PATH, CANDIDATE_LINE_COUNT, CANDIDATE_PROJECT,
    CANDIDATE_REPOSITORY, CANDIDATE_SIDECAR_SCHEMA,
    _build_candidate_tree, _prepare_empty_root,
)


class CandidateBuildTest(unittest.TestCase):
    def _identity(self):
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=os.getcwd()
        ).decode("ascii").strip()
        return {
            "commit_sha": commit,
            "build_id": "candidate-build-test",
            "asset_hash": "a" * 64,
        }

    @staticmethod
    def _bootstrap_provenance(identity):
        return {
            "provenance_class": "served-root-bootstrap",
            "provenance_schema_version": 1,
            "source_commit_sha": identity["commit_sha"],
            "source_tree_sha": "d" * 40,
            "worktree_clean": True,
            "build_workflow_identity": "candidate-build-test",
            "build_workflow_run_id": "1",
            "build_workflow_run_attempt": "1",
            "build_workflow_sha": "f" * 40,
            "source_manifest_sha256": "1" * 64,
            "build_input_manifest_sha256": "2" * 64,
        }

    def test_builder_emits_a_publishable_vnext_report_and_sidecar(self):
        self.assertEqual(CANDIDATE_LINE_COUNT, 100000)
        with tempfile.TemporaryDirectory(prefix="candidate-build-") as root:
            candidate = os.path.join(root, "candidate")
            _prepare_empty_root(candidate)
            identity = self._identity()
            details = _build_candidate_tree(
                os.getcwd(), candidate, identity, line_count=32
            )
            normalize_candidate_artifact(candidate)
            manifest = CandidateArtifactManifest.build(
                candidate, identity,
                source_provenance=self._bootstrap_provenance(identity),
            )
            self.assertEqual(manifest["artifact_role"], VALIDATION_FIXTURE_ARTIFACT_ROLE)
            self.assertFalse(manifest["production_publishable"])
            self.assertEqual(manifest["project_name"], CANDIDATE_PROJECT)
            with self.assertRaisesRegex(ValueError, "validation_fixture"):
                build_release_manifest(
                    candidate, identity, "candidate-build-test-session",
                    candidate_sha=identity["commit_sha"],
                    candidate_artifact_manifest=manifest,
                )
            self.assertEqual(details["project_name"], CANDIDATE_PROJECT)
            self.assertEqual(details["repository_name"], CANDIDATE_REPOSITORY)
            self.assertEqual(details["file_path"], CANDIDATE_FILE_PATH)
            self.assertEqual(details["line_count"], 32)

            report_path = os.path.join(
                candidate, "reports", "coverage_candidate.gcov.html"
            )
            with open(report_path, "r", encoding="utf-8") as stream:
                html = stream.read()
            self.assertIn(
                '<meta name="coverage-report-mode" content="VNEXT_ARTIFACT_READY">',
                html,
            )
            self.assertIn('<pre class="source"></pre>', html)
            self.assertIn('src="coverage_enhance.js"', html)
            self.assertIn('href="coverage_enhance.css"', html)
            self.assertNotIn("Trusted Candidate Build", html)

            report_id = details["report_id"]
            sidecar_key = calc_sidecar_file_key(
                CANDIDATE_FILE_PATH, CANDIDATE_REPOSITORY
            )
            meta_path = os.path.join(
                candidate, "reports", ".source_cache", report_id,
                sidecar_key, "meta.json"
            )
            with open(meta_path, "r", encoding="utf-8") as stream:
                metadata = json.load(stream)
            self.assertEqual(metadata["schema_version"], CANDIDATE_SIDECAR_SCHEMA)
            self.assertEqual(metadata["report_id"], report_id)
            self.assertEqual(metadata["total_lines"], 32)
            self.assertTrue(metadata["chunks"])

            registry_path = os.path.join(
                candidate, "registry", report_id + ".json"
            )
            with open(registry_path, "r", encoding="utf-8") as stream:
                registry = json.load(stream)
            self.assertEqual(registry["report_mode"], "VNEXT_ARTIFACT_READY")
            self.assertEqual(registry["sidecar_schema"], CANDIDATE_SIDECAR_SCHEMA)
            self.assertEqual(registry["report_root"], "reports")


if __name__ == "__main__":
    unittest.main()
