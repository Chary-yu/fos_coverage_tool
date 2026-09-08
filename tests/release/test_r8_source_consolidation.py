import json
import os
import tempfile
import unittest
from unittest import mock

from scripts.upgrade.run_upgrade import (
    CandidateValidationError, UpgradeOrchestrator,
)
from scripts.upgrade.validation_session import ValidationSession
from scripts.diagnostics.upgrade_failure_summary import build_summary


class R8SourceConsolidationTest(unittest.TestCase):
    def _orchestrator(self):
        root = tempfile.TemporaryDirectory(prefix="r8-source-test-")
        self.addCleanup(root.cleanup)
        orchestrator = UpgradeOrchestrator(repo_root=root.name)
        orchestrator._target_identity = {"commit_sha": "a" * 40}
        orchestrator._upgrade_mode = "production"
        orchestrator._runtime_config = {
            "upgrade": {
                "validation_start_timeout_sec": 1,
                "validation_poll_interval_sec": 0.05,
            }
        }
        manifest = os.path.join(root.name, "validation.json")
        orchestrator.validation_session = ValidationSession.create(
            manifest, "candidate-r8", candidate_sha="a" * 40,
            baseline_sha="b" * 40, ports=[19528],
        )
        orchestrator.validation_session_manifest_path = manifest
        orchestrator.release_validation_session_id = "candidate-r8"
        return orchestrator, root.name

    def test_process_exit_is_classified_before_http_connection_refused(self):
        orchestrator, _root = self._orchestrator()
        adapter = mock.Mock()
        adapter.validation_runtime_status.return_value = {
            "status": "PASSED",
            "main_pid": 0,
            "exec_main_pid": 1234,
            "exec_main_status": 1,
            "result": "exit-code",
            "active_state": "failed",
            "process_exited": True,
        }
        orchestrator._production_lifecycle_adapter = adapter
        with self.assertRaises(CandidateValidationError) as raised:
            orchestrator._wait_candidate_validation_ready(
                "http://127.0.0.1:19528/api/coverage/release",
                {"commit_sha": "a" * 40},
                {"status": "PASSED"},
            )
        self.assertEqual(raised.exception.stage, "VALIDATION_PROCESS_EXITED")

    def test_failed_status_probe_is_not_treated_as_start_timeout(self):
        orchestrator, _root = self._orchestrator()
        adapter = mock.Mock()
        adapter.validation_runtime_status.return_value = {
            "status": "FAILED",
            "main_pid": 0,
            "process_exited": False,
        }
        orchestrator._production_lifecycle_adapter = adapter
        with self.assertRaises(CandidateValidationError) as raised:
            orchestrator._wait_candidate_validation_ready(
                "http://127.0.0.1:19528/api/coverage/release",
                {"commit_sha": "a" * 40},
                {"status": "PASSED"},
            )
        self.assertEqual(
            raised.exception.stage, "VALIDATION_STATUS_PROBE_FAILED"
        )

    def test_pid_is_registered_before_endpoint_readiness(self):
        orchestrator, _root = self._orchestrator()
        pid = os.getpid()
        adapter = mock.Mock()
        adapter.validation_runtime_status.return_value = {
            "status": "PASSED",
            "main_pid": pid,
            "exec_main_pid": pid,
            "exec_main_status": 0,
            "result": "success",
            "active_state": "active",
            "process_exited": False,
        }
        orchestrator._production_lifecycle_adapter = adapter
        endpoint_evidence = {
            "status": "PASSED",
            "release": {"commit_sha": "a" * 40},
        }
        with mock.patch(
                "scripts.upgrade.run_upgrade._endpoint_tcp_ready",
                side_effect=[False, True]), mock.patch.object(
                    orchestrator, "_verify_release_endpoint",
                    return_value=endpoint_evidence):
            result = orchestrator._wait_candidate_validation_ready(
                "http://127.0.0.1:19528/api/coverage/release",
                {"commit_sha": "a" * 40},
                {"status": "PASSED"},
            )
        self.assertEqual(result, endpoint_evidence)
        persisted = orchestrator.validation_session.data
        self.assertIn(pid, persisted["pids"])
        self.assertEqual(persisted["validation_status"], "PASSED")
        self.assertTrue(persisted["listeners"])

    def test_failure_evidence_is_recorded_before_teardown(self):
        orchestrator, _root = self._orchestrator()
        adapter = mock.Mock()
        adapter.validation_failure_snapshot.return_value = {
            "status": "PASSED",
            "runtime_status": {"exec_main_status": 1},
            "credentials_written_to_evidence": False,
        }
        orchestrator._production_lifecycle_adapter = adapter
        orchestrator._capture_validation_failure(
            "VALIDATION_PROCESS_EXITED", RuntimeError("synthetic")
        )
        self.assertEqual(
            orchestrator.validation_session.data["validation_status"], "FAILED"
        )
        self.assertEqual(
            orchestrator.validation_session.data["validation_failure_stage"],
            "VALIDATION_PROCESS_EXITED",
        )
        evidence = orchestrator.manifest.data["validation_failure"]
        self.assertTrue(evidence["captured_before_teardown"])
        self.assertEqual(evidence["failure_stage"], "VALIDATION_PROCESS_EXITED")

    def test_failure_summary_prioritizes_runtime_and_bounds_report_noise(self):
        reports = [
            {
                "report_id": "report-{}".format(index),
                "report_mode": "LEGACY_STATIC",
                "sidecar_schema": 0,
            }
            for index in range(5000)
        ]
        manifest = {
            "status": "UNMET_GATES",
            "release_decision": "NOT_READY",
            "validation_failure": {
                "failure_stage": "VALIDATION_PROCESS_EXITED",
                "journal": "password=do-not-leak",
            },
            "candidate_release_prepared": {
                "release_manifest": {"reports": reports}
            },
        }
        summary = build_summary(manifest)
        encoded = json.dumps(summary, sort_keys=True)
        self.assertIn("VALIDATION_PROCESS_EXITED", encoded)
        self.assertNotIn("do-not-leak", encoded)
        self.assertEqual(summary["report_summary"]["report_count"], 5000)
        self.assertFalse(summary["report_summary"]["entries_expanded"])
        self.assertNotIn("candidate_release_prepared", summary["priority"])

    def test_upgrade_state_is_atomic_and_terminal_status_is_explicit(self):
        orchestrator, root = self._orchestrator()
        orchestrator.upgrade_state_path = os.path.join(root, "upgrade-state.json")
        orchestrator._write_upgrade_state(
            "FAILED_VALIDATION_PROCESS_EXITED",
            failure_stage="VALIDATION_PROCESS_EXITED",
            failure_reason="candidate runtime exited",
        )
        with open(orchestrator.upgrade_state_path, encoding="utf-8") as stream:
            state = json.load(stream)
        self.assertEqual(state["state"], "FAILED_VALIDATION_PROCESS_EXITED")
        self.assertEqual(state["target_sha"], "a" * 40)
        self.assertEqual(state["release_validation_session_id"], "candidate-r8")
        self.assertEqual(state["failure_stage"], "VALIDATION_PROCESS_EXITED")


if __name__ == "__main__":
    unittest.main()
