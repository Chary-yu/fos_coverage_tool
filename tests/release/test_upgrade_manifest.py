import hashlib
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.upgrade.build_deployment_manifest import build
from scripts.upgrade.cutover_controller import CutoverController
from scripts.upgrade.run_upgrade import (
    _new_release_validation_session_id, _resolve_attempt_path,
    _validate_candidate_browser_evidence,
    validate_candidate_publication_preflight,
    verify_production_candidate_served_root_binding,
)


class TestUpgradeManifest(unittest.TestCase):
    def test_candidate_preflight_rejects_workflow_sha_placeholder_before_maintenance(self):
        with self.assertRaisesRegex(RuntimeError, "still a placeholder"):
            validate_candidate_publication_preflight(
                os.getcwd(), os.path.join(os.getcwd(), "missing-candidate"),
                {"commit_sha": "a" * 40}, "", "github-actions/candidate-build",
                "REPLACE_WITH_TRUSTED_BUILD_WORKFLOW_COMMIT_SHA",
            )

    def test_candidate_preflight_rejects_missing_manifest_before_maintenance(self):
        with tempfile.TemporaryDirectory(prefix="candidate-preflight-") as root:
            with self.assertRaisesRegex(RuntimeError, "candidate artifact manifest is missing"):
                validate_candidate_publication_preflight(
                    os.getcwd(), root,
                    {"commit_sha": "a" * 40}, "", "github-actions/candidate-build",
                    "b" * 40,
                    "", "", "Chary-yu/fos_coverage_tool",
                    "Chary-yu/fos_coverage_tool/.github/workflows/ci.yml",
                )

    def test_candidate_from_current_b_cannot_upgrade_current_a(self):
        current_a = {
            "previous_release_commit_sha": "a" * 40,
            "served_root_tree_sha256": "b" * 64,
            "served_root_identity_sha256": "c" * 64,
        }
        candidate_from_b = {
            "previous_release_commit_sha": "d" * 40,
            "served_root_tree_sha256": "e" * 64,
            "served_root_identity_sha256": "f" * 64,
        }
        with self.assertRaisesRegex(RuntimeError, "previous_release_commit_sha"):
            verify_production_candidate_served_root_binding(
                candidate_from_b, current_a
            )

    def test_candidate_binding_matches_current(self):
        current = {
            "previous_release_commit_sha": "a" * 40,
            "served_root_tree_sha256": "b" * 64,
            "served_root_identity_sha256": "c" * 64,
        }
        self.assertEqual(
            verify_production_candidate_served_root_binding(current, current),
            current,
        )

    def test_production_upgrade_uses_immutable_publication_only(self):
        with open(
                os.path.join(ROOT, "scripts", "upgrade", "run_upgrade.py"),
                encoding="utf-8") as stream:
            source = stream.read()
        self.assertIn("ImmutableReleasePublisher", source)
        self.assertIn("self.publisher.prepare", source)
        self.assertIn("self.publisher.switch_current", source)
        self.assertNotIn("from scripts.upgrade.cutover_controller import", source)
        self.assertNotIn("self.cutover.apply", source)

    def test_explicit_manifest_hash_and_rollback(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "candidate.txt")
            with open(path, "w") as stream:
                stream.write("candidate")
            manifest_path = os.path.join(root, "manifest.json")
            build(root, ["candidate.txt"], manifest_path)
            with open(manifest_path) as stream:
                manifest = json.load(stream)
            self.assertEqual(manifest["actions"][0]["op"], "ADD")
            controller = CutoverController(root, os.path.join(root, "backup"))
            controller.apply(manifest["actions"])
            with open(path) as stream:
                self.assertEqual(stream.read(), "candidate")

    def test_source_hash_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "candidate.txt")
            with open(path, "w") as stream:
                stream.write("one")
            manifest = {"actions": [{"op": "ADD", "source": "candidate.txt",
                                     "destination": "candidate.txt", "source_sha256": "bad",
                                     "backup_required": True}]}
            with self.assertRaises(RuntimeError):
                CutoverController(root, os.path.join(root, "backup")).apply(manifest["actions"])

    def test_fixture_browser_payload_cannot_be_candidate_evidence(self):
        identity = {
            "version": "v-test",
            "commit_sha": "a" * 40,
            "build_id": "build-a",
            "asset_hash": "b" * 64,
            "schema_version": 2,
            "asset_manifest_version": 1,
            "asset_count": 1,
            "asset_manifest_hash": "c" * 64,
            "asset_manifest": [{"path": "coverage.js"}],
        }
        with tempfile.TemporaryDirectory() as root:
            artifact = os.path.join(root, "browser-report.json")
            with open(artifact, "w", encoding="utf-8") as stream:
                stream.write("report")
            payload = {
                "status": "PASSED",
                "evidence_class": "real_http_chromium_fixture",
                "release_eligible": False,
                "synthetic": True,
                "candidate_revision": identity["commit_sha"],
                "page_url": "http://127.0.0.1:19528/",
                "release_identity": identity,
                "browser_functional": {"status": "PASSED"},
                "coverage_virtual_scroll_100k": {
                    "status": "PASSED",
                    "workload_id": "fixture",
                    "environment_identity": {"browser_name": "chromium"},
                },
                "artifact_path": artifact,
                "artifact_sha256": hashlib.sha256(b"report").hexdigest(),
            }
            errors, normalized = _validate_candidate_browser_evidence(
                artifact, payload, identity, "http://127.0.0.1:19528/"
            )
        self.assertTrue(errors)
        self.assertEqual(normalized["status"], "FAILED")
        self.assertFalse(normalized["release_eligible"])

    def test_real_candidate_browser_evidence_binds_url_revision_and_artifact(self):
        identity = {
            "version": "v-test",
            "commit_sha": "a" * 40,
            "build_id": "build-a",
            "asset_hash": "b" * 64,
            "schema_version": 2,
            "asset_manifest_version": 1,
            "asset_count": 1,
            "asset_manifest_hash": "c" * 64,
            "asset_manifest": [{"path": "coverage.js"}],
        }
        with tempfile.TemporaryDirectory() as root:
            artifact = os.path.join(root, "browser-report.json")
            with open(artifact, "w", encoding="utf-8") as stream:
                stream.write("report")
            artifact_sha = hashlib.sha256(b"report").hexdigest()
            payload = {
                "status": "PASSED",
                "evidence_class": "real_http_chromium_browser",
                "release_eligible": True,
                "synthetic": False,
                "release_validation_session_id": "candidate-attempt-1",
                "candidate_artifact_sha256": "d" * 64,
                "served_root_sha256": "e" * 64,
                "candidate_revision": identity["commit_sha"],
                "page_url": "https://candidate.example.invalid/report.html",
                "release_identity": identity,
                "browser_functional": {"status": "PASSED"},
                "coverage_virtual_scroll_100k": {
                    "status": "PASSED",
                    "workload_id": "real-workload",
                    "environment_identity": {"browser_name": "chromium"},
                },
                "artifact_path": artifact,
                "artifact_sha256": artifact_sha,
            }
            errors, normalized = _validate_candidate_browser_evidence(
                artifact, payload, identity,
                "https://candidate.example.invalid/report.html",
                expected_session_id="candidate-attempt-1",
                expected_candidate_artifact_sha256="d" * 64,
                expected_served_root_sha256="e" * 64,
            )
        self.assertEqual(errors, [])
        self.assertEqual(normalized["evidence_class"], "real_candidate_browser")
        self.assertTrue(normalized["real_http"])
        self.assertTrue(normalized["chromium"])
        self.assertEqual(normalized["browser_artifact_sha256"], artifact_sha)
        self.assertEqual(normalized["release_validation_session_id"], "candidate-attempt-1")

    def test_candidate_browser_evidence_rejects_a_different_attempt(self):
        identity = {
            "commit_sha": "a" * 40,
            "build_id": "build-a",
        }
        payload = {
            "status": "PASSED",
            "evidence_class": "real_http_chromium_browser",
            "release_eligible": True,
            "synthetic": False,
            "candidate_revision": identity["commit_sha"],
            "release_validation_session_id": "attempt-a",
            "candidate_artifact_sha256": "d" * 64,
            "served_root_sha256": "e" * 64,
            "page_url": "https://candidate.example.invalid/",
            "release_identity": identity,
            "browser_functional": {"status": "PASSED"},
            "coverage_virtual_scroll_100k": {
                "status": "PASSED",
                "environment_identity": {"browser_name": "chromium"},
            },
        }
        errors, _ = _validate_candidate_browser_evidence(
            "unused.json", payload, identity, "https://candidate.example.invalid/",
            expected_session_id="attempt-b",
            expected_candidate_artifact_sha256="d" * 64,
            expected_served_root_sha256="e" * 64,
        )
        self.assertIn("release_validation_session_id", " ".join(errors))

    def test_same_candidate_sha_gets_independent_attempt_ids_and_paths(self):
        revision = "a" * 40
        first = _new_release_validation_session_id(revision)
        second = _new_release_validation_session_id(revision)
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("candidate-{}-".format(revision)))
        self.assertTrue(second.startswith("candidate-{}-".format(revision)))
        self.assertEqual(
            _new_release_validation_session_id(revision, "operator-attempt"),
            "operator-attempt",
        )
        with tempfile.TemporaryDirectory() as root:
            path = _resolve_attempt_path(
                root, "state/session-{attempt_id}.json",
                "validation_session_manifest", first,
            )
            self.assertIn(first, path)
            self.assertNotEqual(
                path,
                _resolve_attempt_path(
                    root, "state/session-{attempt_id}.json",
                    "validation_session_manifest", second,
                ),
            )
            fixed_first = _resolve_attempt_path(
                root, "artifacts/browser.json", "candidate_browser_evidence_path", first
            )
            fixed_second = _resolve_attempt_path(
                root, "artifacts/browser.json", "candidate_browser_evidence_path", second
            )
            self.assertIn(first, fixed_first)
            self.assertIn(second, fixed_second)
            self.assertNotEqual(fixed_first, fixed_second)


if __name__ == "__main__":
    unittest.main()
