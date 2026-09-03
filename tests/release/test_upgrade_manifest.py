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
    UpgradeOrchestrator,
    validate_candidate_publication_preflight,
    verify_production_candidate_served_root_binding,
)
from scripts.upgrade.evidence_manifest import ProductionEvidenceManifest


class TestUpgradeManifest(unittest.TestCase):
    @staticmethod
    def _pre_cutover_data():
        revision = "a" * 40
        session = "candidate-attempt-1"
        artifact = "b" * 64
        served = "c" * 64
        passed = {"status": "PASSED", "revision": revision, "exit_code": 0}
        return {
            "release_identity": {
                "version": "v-test", "commit_sha": revision,
                "build_id": "build-test",
            },
            "backup_evidence": dict(
                passed, evidence_class="blue_green_database",
                full_sql_gz_sha256="d" * 64,
            ),
            "database_generation": dict(
                passed, generation="VNEXT",
            ),
            "disposable_target": dict(
                passed,
                target_database="coverage_vnext_candidate_test",
                target_database_created_by_this_run=True,
                candidate_access={
                    "status": "PASSED",
                    "grant_applied_by_run": True,
                    "pre_grant_privilege_snapshot": {
                        "account": "coverage_user@127.0.0.1",
                        "grants": ["GRANT USAGE ON *.*"],
                    },
                },
            ),
            "database_separation": dict(passed),
            "schema_migration": dict(passed, preflight_safe=True),
            "candidate_release_prepared": dict(
                passed, current_unchanged=True,
                release_validation_session_id=session,
                candidate_artifact_sha256=artifact,
                served_root_sha256=served,
            ),
            "data_hash_verification": dict(
                passed, verified=True,
                source_semantic_hash="e" * 64,
                target_semantic_hash="e" * 64,
            ),
            "file_state_gate": dict(
                passed, conditions_passed=True, project_count=1,
                project_gates=[{
                    "status": "PASSED",
                    "explicit_conditions": {
                        "expected_file_count_equals_file_state_count": True,
                        "missing_file_count_zero": True,
                        "orphan_file_state_count_zero": True,
                        "stale_file_count_zero": True,
                        "pending_conservation": True,
                        "authoritative_reconciliation": True,
                        "file_state_version_equals_data_version": True,
                    },
                    "data_version": 7,
                    "file_state_version": 7,
                    "completeness": {
                        "status": "PASSED",
                        "expected_file_count": 1,
                        "state_file_count": 1,
                        "missing_file_count": 0,
                        "orphan_file_state_count": 0,
                        "stale_file_count": 0,
                    },
                    "pending_conservation": {
                        "status": "PASSED",
                        "pending_total": 1,
                        "ordinary_pending_total": 1,
                        "inherited_pending_total": 0,
                        "manual_draft_pending_total": 0,
                    },
                    "reconciliation": {"status": "PASSED"},
                }],
            ),
            "targeted_tests": {
                "tests.release.test_upgrade_manifest": dict(passed),
            },
            "browser_fixture_regression": dict(
                passed, evidence_class="browser_fixture_regression",
            ),
            "candidate_browser_evidence": dict(
                passed, evidence_class="real_candidate_browser",
                release_validation_session_id=session,
                candidate_artifact_sha256=artifact,
                served_root_sha256=served,
                expected_commit_sha=revision,
                release_eligible=True, synthetic=False,
                real_http=True, chromium=True,
            ),
            "performance_benchmark": dict(
                passed, evidence_class="release_performance_ab",
                candidate_commit=revision,
                release_validation_session_id=session,
                candidate_artifact_sha256=artifact,
                served_root_sha256=served,
            ),
            "path_mapping_audit": dict(
                passed, is_valid=True, input_kind="repository_lcov",
            ),
            "sidecar_audit": dict(passed, is_safe=True),
            "security_audit": dict(
                passed, is_safe=True, critical_count=0, high_count=0,
            ),
            "candidate_release_endpoint": dict(
                passed, process_role="validation_candidate",
            ),
            "api_start": dict(passed, process_role="validation_candidate"),
            "validation_session_manifest": dict(
                passed, session_id=session, candidate_sha=revision,
                artifact_sha256="f" * 64,
            ),
            "validation_teardown": dict(
                passed, session_id=session, pids_closed=True,
                ports_closed=True, ports_probe_ok=True,
            ),
            "rollback_evidence": dict(
                passed, rehearsal_verified=True,
                before_release_id="before-session",
                target_release_id=session,
                rollback_release_id="before-session",
                release_validation_session_id=session,
                candidate_artifact_sha256=artifact,
                served_root_sha256=served,
            ),
        }

    def _manifest_for_pre_cutover(self, root, data):
        manifest = ProductionEvidenceManifest(root)
        manifest.data.update(data)
        return manifest

    def test_missing_rollback_artifact_blocks_phase_d_methods(self):
        with tempfile.TemporaryDirectory(prefix="pre-cutover-rollback-") as root:
            manifest = self._manifest_for_pre_cutover(root, self._pre_cutover_data())
            manifest.data.pop("rollback_evidence")
            orchestrator = UpgradeOrchestrator(repo_root=root)
            orchestrator.manifest = manifest
            phase_d_methods = []
            ready, _unmet = orchestrator._validate_pre_cutover_ready(
                manifest.data["release_identity"], "staging"
            )
            if ready:
                phase_d_methods.extend(("freeze", "stop_current", "switch_current"))
            self.assertFalse(ready)
            self.assertEqual(phase_d_methods, [])
            self.assertEqual(
                manifest.data["pre_cutover_ready"]["status"], "FAILED"
            )

    def test_complete_pre_cutover_evidence_can_pass_without_phase_d_methods(self):
        with tempfile.TemporaryDirectory(prefix="pre-cutover-pass-") as root:
            manifest = self._manifest_for_pre_cutover(
                root, self._pre_cutover_data()
            )
            orchestrator = UpgradeOrchestrator(repo_root=root)
            orchestrator.manifest = manifest
            ready, unmet = orchestrator._validate_pre_cutover_ready(
                manifest.data["release_identity"], "staging"
            )
            self.assertTrue(ready, unmet)
            self.assertEqual(
                manifest.data["pre_cutover_ready"]["status"], "PASSED"
            )
            self.assertFalse(
                manifest.data["pre_cutover_ready"]["phase_d_entered"]
            )

    def test_failed_path_mapping_blocks_phase_d_methods(self):
        with tempfile.TemporaryDirectory(prefix="pre-cutover-path-") as root:
            data = self._pre_cutover_data()
            data["path_mapping_audit"].update({
                "status": "FAILED", "is_valid": False, "exit_code": 1,
            })
            manifest = self._manifest_for_pre_cutover(root, data)
            orchestrator = UpgradeOrchestrator(repo_root=root)
            orchestrator.manifest = manifest
            phase_d_methods = []
            ready, _unmet = orchestrator._validate_pre_cutover_ready(
                manifest.data["release_identity"], "staging"
            )
            if ready:
                phase_d_methods.extend(("freeze", "stop_current", "switch_current"))
            self.assertFalse(ready)
            self.assertEqual(phase_d_methods, [])
            self.assertEqual(
                manifest.data["pre_cutover_ready"]["status"], "FAILED"
            )

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
        self.assertIn("self.publisher.validate_current(persist=False)", source)
        self.assertIn("self.publisher.switch_current", source)
        self.assertNotIn("from scripts.upgrade.cutover_controller import", source)
        self.assertNotIn("self.cutover.apply", source)

    def test_candidate_gates_run_before_phase_d_cutover_mutations(self):
        with open(
                os.path.join(ROOT, "scripts", "upgrade", "run_upgrade.py"),
                encoding="utf-8") as stream:
            source = stream.read()
        prepared = source.index("prepared = self.publisher.prepare")
        validation = source.index("lifecycle.start_validation_api()")
        candidate_endpoint = source.index('"candidate_release_endpoint"')
        targeted = source.index("# Step 5: Run Targeted Unit Test Suites")
        browser = source.index("# Step 6: Run Node DOM & Event-loop Smoke Suite")
        pre_cutover_gate = source.index("self._validate_pre_cutover_ready(identity, mode)")
        freeze = source.index("lifecycle.freeze(identity.get(\"commit_sha\", \"\"))")
        stop = source.index("lifecycle.stop_current_api()")
        switched = source.index("self.publisher.switch_current(session_id)")
        self.assertLess(prepared, validation)
        self.assertLess(validation, candidate_endpoint)
        self.assertLess(validation, targeted)
        self.assertLess(targeted, browser)
        self.assertLess(browser, freeze)
        self.assertLess(pre_cutover_gate, freeze)
        self.assertLess(freeze, stop)
        self.assertLess(stop, switched)
        self.assertIn("current_unchanged_until_phase_d", source)

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
