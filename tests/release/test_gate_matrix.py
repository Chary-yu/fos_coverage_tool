import json
import os
import tempfile
import unittest

from scripts.diagnostics.gate_matrix import _external, _revision


class GateMatrixEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
        self.revision = _revision(self.repo_root)

    def test_external_path_must_exist_and_be_authentic_json(self):
        old = os.environ.get("COVERAGE_GATE_TEST_EVIDENCE")
        try:
            os.environ["COVERAGE_GATE_TEST_EVIDENCE"] = os.path.join(
                tempfile.gettempdir(), "coverage-gate-evidence-does-not-exist.json"
            )
            result = _external(
                "test", "test evidence", "COVERAGE_GATE_TEST_EVIDENCE",
                self.revision, self.repo_root,
            )
            self.assertEqual(result["status"], "INCOMPLETE")
            self.assertTrue(result["violations"])
        finally:
            if old is None:
                os.environ.pop("COVERAGE_GATE_TEST_EVIDENCE", None)
            else:
                os.environ["COVERAGE_GATE_TEST_EVIDENCE"] = old

    def test_external_pass_requires_candidate_identity_and_non_synthetic_record(self):
        fd, path = tempfile.mkstemp(prefix="coverage-gate-evidence-", suffix=".json")
        os.close(fd)
        old = os.environ.get("COVERAGE_GATE_TEST_EVIDENCE")
        try:
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({
                    "status": "PASSED",
                    "candidate_revision": self.revision,
                    "host_identity": {"hostname": "test"},
                    "command_or_action": "test evidence",
                    "synthetic": False,
                }, stream)
            os.environ["COVERAGE_GATE_TEST_EVIDENCE"] = path
            result = _external(
                "test", "test evidence", "COVERAGE_GATE_TEST_EVIDENCE",
                self.revision, self.repo_root,
            )
            self.assertEqual(result["status"], "PASSED")
            self.assertEqual(result["violations"], [])
        finally:
            if old is None:
                os.environ.pop("COVERAGE_GATE_TEST_EVIDENCE", None)
            else:
                os.environ["COVERAGE_GATE_TEST_EVIDENCE"] = old
            try:
                os.remove(path)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
