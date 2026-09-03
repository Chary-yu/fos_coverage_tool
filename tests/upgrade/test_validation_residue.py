import os
import tempfile
import unittest

from scripts.upgrade.validation_residue import (
    BLOCKED, PASSED, SAFE_TO_TEARDOWN,
    scan_validation_residue, teardown_validation_residue,
)


class ValidationResidueGateTest(unittest.TestCase):
    def _owned_fixture(self):
        root = tempfile.TemporaryDirectory(prefix="validation-residue-")
        self.addCleanup(root.cleanup)
        candidate = os.path.join(root.name, "gate_old")
        os.makedirs(candidate)
        session = "candidate-session-1"
        process = {
            "pid": 43123,
            "cmdline": "candidate-api --root {} --session {} --port 19528".format(
                candidate, session
            ),
            "cwd": candidate,
            "candidate_root": candidate,
            "port": 19528,
            "session_identity": session,
        }
        listener = {"pid": 43123, "port": 19528}
        return root, candidate, session, process, listener

    def test_all_identity_fields_are_required_before_teardown(self):
        _root, candidate, session, process, listener = self._owned_fixture()
        report = scan_validation_residue(
            candidate_roots=[os.path.dirname(candidate)], ports=[19528],
            session_identity=session, processes=[process], listeners=[listener],
        )
        self.assertEqual(report["status"], SAFE_TO_TEARDOWN)
        killed = []
        teardown = teardown_validation_residue(
            [os.path.dirname(candidate)], [19528], session,
            processes=[process], listeners=[listener],
            killer=lambda pid, _signal: killed.append(pid),
        )
        self.assertEqual(teardown["teardown_status"], PASSED)
        self.assertEqual(killed, [43123])

    def test_mismatched_session_or_port_is_blocked_without_kill(self):
        _root, candidate, session, process, listener = self._owned_fixture()
        bad_process = dict(process, session_identity="other-session", port=9528)
        report = scan_validation_residue(
            candidate_roots=[os.path.dirname(candidate)], ports=[19528],
            session_identity=session, processes=[bad_process], listeners=[listener],
        )
        self.assertEqual(report["status"], BLOCKED)
        self.assertFalse(report["teardown_authorized"])
        killed = []
        teardown = teardown_validation_residue(
            [os.path.dirname(candidate)], [19528], session,
            processes=[bad_process], listeners=[listener],
            killer=lambda pid, _signal: killed.append(pid),
        )
        self.assertEqual(teardown["teardown_status"], BLOCKED)
        self.assertEqual(killed, [])

    def test_empty_configured_scan_is_passed(self):
        report = scan_validation_residue(
            candidate_roots=[], ports=[], session_identity="session",
            processes=[], listeners=[],
        )
        self.assertEqual(report["status"], PASSED)


if __name__ == "__main__":
    unittest.main()
