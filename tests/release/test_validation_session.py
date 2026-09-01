import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts.upgrade.validation_session import (
    ValidationSession,
    _pid_exists,
    _proc_start_time,
    validate_bind,
)
from scripts.upgrade import local_staging_control


class ValidationSessionTest(unittest.TestCase):
    def test_script_entrypoints_work_when_invoked_by_path(self):
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
        for relative_path in (
                "scripts/upgrade/validation_session.py",
                "scripts/upgrade/local_staging_control.py"):
            output = subprocess.check_output(
                [sys.executable, relative_path, "--help"],
                cwd=repo_root, stderr=subprocess.STDOUT,
                universal_newlines=True,
            )
            self.assertIn("usage:", output.lower())

    def test_staging_start_rejects_unowned_live_pid_file(self):
        with tempfile.TemporaryDirectory(prefix="validation-session-start-") as root:
            config_path = os.path.join(root, "config.json")
            pid_path = os.path.join(root, "api.pid")
            with open(config_path, "w", encoding="utf-8") as stream:
                json.dump({"server": {"host": "127.0.0.1", "port": 19528}}, stream)
            with open(pid_path, "w", encoding="utf-8") as stream:
                stream.write(str(os.getpid()))
            with mock.patch.object(local_staging_control.subprocess, "Popen") as popen:
                with self.assertRaisesRegex(RuntimeError, "not owned"):
                    local_staging_control.start(
                        config_path, pid_path,
                        "http://127.0.0.1:19528/api/coverage/release",
                    )
            popen.assert_not_called()

    def test_staging_stop_without_manifest_never_signals_pid_file(self):
        with tempfile.TemporaryDirectory(prefix="validation-session-stop-") as root:
            pid_path = os.path.join(root, "api.pid")
            with open(pid_path, "w", encoding="utf-8") as stream:
                stream.write(str(os.getpid()))
            with mock.patch.object(local_staging_control.os, "kill") as kill:
                result = local_staging_control.stop(pid_path)
            self.assertEqual(result["status"], "FAILED")
            kill.assert_not_called()

    def test_loopback_is_default_and_non_loopback_requires_explicit_controls(self):
        self.assertEqual(
            validate_bind("127.0.0.1", 19528)["exposure"], "loopback"
        )
        with self.assertRaisesRegex(ValueError, "loopback"):
            validate_bind("0.0.0.0", 19529)
        with self.assertRaisesRegex(ValueError, "allowlist"):
            validate_bind(
                "0.0.0.0", 19529, allow_non_loopback=True,
                temporary_token="temporary", expires_at="2026-08-28T12:00:00Z",
            )
        result = validate_bind(
            "0.0.0.0", 19529, allow_non_loopback=True,
            allowlist=["127.0.0.1"], temporary_token="temporary",
            expires_at="2026-08-28T12:00:00Z",
        )
        self.assertEqual(result["exposure"], "explicit_non_loopback")
        self.assertEqual(result["allowlist"], ["127.0.0.1"])

    def test_manifest_records_identity_and_empty_teardown_evidence(self):
        with tempfile.TemporaryDirectory(prefix="validation-session-") as root:
            manifest_path = os.path.join(root, "session.json")
            evidence_path = os.path.join(root, "teardown.json")
            session = ValidationSession.create(
                manifest_path, "candidate-session", candidate_sha="a" * 40,
                baseline_sha="b" * 40,
                ports=[19528],
                binds=[{"host": "127.0.0.1", "port": 19528}],
            )
            loaded = ValidationSession.load(manifest_path)
            self.assertEqual(loaded.data["candidate_sha"], "a" * 40)
            self.assertEqual(loaded.data["baseline_sha"], "b" * 40)
            self.assertEqual(loaded.data["ports"], [19528])

            with mock.patch(
                    "scripts.upgrade.validation_session._port_listeners",
                    return_value=[]):
                evidence = loaded.teardown(evidence_path=evidence_path, timeout=0)
                self.assertEqual(evidence["status"], "PASSED")
                self.assertTrue(evidence["pids_closed"])
                self.assertTrue(evidence["ports_closed"])
                self.assertTrue(evidence["ports_probe_ok"])
                self.assertFalse(evidence["p1"])
            with open(evidence_path, encoding="utf-8") as stream:
                persisted = json.load(stream)
            self.assertEqual(persisted["session_id"], "candidate-session")
            self.assertTrue(persisted["pids_closed"])
            self.assertTrue(persisted["ports_closed"])
            self.assertEqual(
                ValidationSession.load(manifest_path).data["teardown_status"],
                "PASSED",
            )

    def test_pid_reuse_is_reported_as_teardown_failure_without_signaling(self):
        with tempfile.TemporaryDirectory(prefix="validation-session-reuse-") as root:
            manifest_path = os.path.join(root, "session.json")
            session = ValidationSession.create(
                manifest_path, "reuse-session", pids=[os.getpid()]
            )
            expected = session.data["pid_start_times"][str(os.getpid())]
            with mock.patch(
                "scripts.upgrade.validation_session._proc_start_time",
                return_value=int(expected) + 1,
            ), mock.patch(
                "scripts.upgrade.validation_session._pid_exists",
                return_value=True,
            ):
                verification = session.verify_teardown()
            self.assertEqual(verification["status"], "FAILED")
            self.assertEqual(verification["reused_pids"], [os.getpid()])
            self.assertFalse(verification["pids_closed"])

    def test_pid_absent_at_creation_is_never_signalable_after_reuse(self):
        reused_pid = 987654321
        with tempfile.TemporaryDirectory(prefix="validation-session-unowned-") as root:
            manifest_path = os.path.join(root, "session.json")
            with mock.patch(
                    "scripts.upgrade.validation_session._pid_exists",
                    return_value=False):
                session = ValidationSession.create(
                    manifest_path, "unowned-session", pids=[reused_pid]
                )
            self.assertNotIn(str(reused_pid), session.data["pid_start_times"])
            with mock.patch(
                    "scripts.upgrade.validation_session._pid_exists",
                    return_value=True), mock.patch(
                    "scripts.upgrade.validation_session._proc_start_time",
                    return_value=123), mock.patch(
                    "scripts.upgrade.validation_session.os.kill") as kill:
                attempted = session.stop_owned_processes(timeout=0)
                verification = session.verify_teardown()
            self.assertEqual(
                attempted, [{"pid": reused_pid, "status": "PID_REUSED"}]
            )
            kill.assert_not_called()
            self.assertEqual(verification["status"], "FAILED")
            self.assertEqual(verification["reused_pids"], [reused_pid])

    def test_zombie_pid_is_not_treated_as_a_live_validation_process(self):
        with mock.patch("scripts.upgrade.validation_session.os.kill"), \
                mock.patch(
                    "scripts.upgrade.validation_session._proc_state",
                    return_value="Z",
                ):
            self.assertFalse(_pid_exists(12345))

    def test_proc_start_time_parses_process_names_with_spaces(self):
        remainder = ["S"] + [str(index) for index in range(4, 53)]
        remainder[19] = "424242"
        stat = "123 (validation worker with spaces) " + " ".join(remainder)
        with mock.patch(
                "builtins.open", mock.mock_open(read_data=stat)):
            self.assertEqual(_proc_start_time(123), 424242)


if __name__ == "__main__":
    unittest.main()
