import os
import subprocess
import unittest

from scripts.diagnostics.dod_status import EXPECTED_DOD_IDS, build
from scripts.diagnostics.task_manifest_audit import EXPECTED_TASKS


class DefinitionOfDoneStatusTest(unittest.TestCase):
    def setUp(self):
        self.repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
        self.revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo_root
        ).decode("ascii").strip()

    def _inputs(self):
        matrix = {
            "candidate_revision": self.revision,
            "gates": {gate: {"status": "PASSED"} for gate in "ABCDEF"},
        }
        tasks = {
            "status": "PASSED",
            "candidate_revision": self.revision,
            "task_count": len(EXPECTED_TASKS),
            "tasks": [
                {"task_id": task_id, "status": "PASSED"}
                for task_id in EXPECTED_TASKS
            ],
        }
        return matrix, tasks

    def test_all_twenty_four_dod_items_require_exact_sha_task_evidence(self):
        matrix, tasks = self._inputs()
        result = build(self.repo_root, matrix=matrix, task_status=tasks)
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["dod_count"], 24)
        self.assertEqual(
            [item["dod_id"] for item in result["items"]], EXPECTED_DOD_IDS
        )
        self.assertTrue(all(item["status"] == "PASSED" for item in result["items"]))

    def test_one_incomplete_required_task_blocks_each_dependent_dod(self):
        matrix, tasks = self._inputs()
        next(item for item in tasks["tasks"] if item["task_id"] == "A-09")["status"] = "INCOMPLETE"
        result = build(self.repo_root, matrix=matrix, task_status=tasks)
        self.assertEqual(result["status"], "INCOMPLETE")
        dod_01 = next(item for item in result["items"] if item["dod_id"] == "DOD-01")
        dod_02 = next(item for item in result["items"] if item["dod_id"] == "DOD-02")
        self.assertEqual(dod_01["status"], "INCOMPLETE")
        self.assertEqual(dod_02["status"], "INCOMPLETE")
        self.assertTrue(any(item["name"] == "task:A-09" for item in dod_01["blockers"]))

    def test_stale_task_status_is_rejected(self):
        matrix, tasks = self._inputs()
        tasks["candidate_revision"] = "0" * 40
        result = build(self.repo_root, matrix=matrix, task_status=tasks)
        self.assertEqual(result["status"], "FAILED")
        self.assertTrue(any("candidate_revision" in item for item in result["violations"]))


if __name__ == "__main__":
    unittest.main()
