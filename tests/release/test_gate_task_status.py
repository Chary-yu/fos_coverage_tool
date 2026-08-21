import os
import unittest

from scripts.diagnostics.gate_task_status import build_from_matrix


class GateTaskStatusTest(unittest.TestCase):
    def setUp(self):
        self.repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))

    @staticmethod
    def _matrix(status="INCOMPLETE"):
        return {
            "candidate_revision": "",
            "release_identity": {},
            "gates": {
                gate: {
                    "status": status,
                    "local_checks": [],
                    "external_evidence": [],
                }
                for gate in "ABCDEF"
            },
        }

    def test_every_frozen_task_is_reported_and_missing_gate_evidence_is_visible(self):
        result = build_from_matrix(self.repo_root, self._matrix())
        self.assertEqual(result["task_count"], 80)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertEqual(result["summary_by_gate"]["A"]["INCOMPLETE"], 10)
        self.assertEqual(len(result["tasks"]), 80)
        self.assertTrue(all(item["status"] == "INCOMPLETE" for item in result["tasks"]))

    def test_task_pass_requires_gate_and_upstream_tasks(self):
        matrix = self._matrix(status="PASSED")
        matrix["gates"]["B"]["local_checks"] = [{
            "name": "target-backfill",
            "status": "INCOMPLETE",
            "evidence_class": "production-database",
            "requirement": "real target DB semantic hash",
            "violations": ["external evidence missing"],
        }]
        result = build_from_matrix(self.repo_root, matrix)
        task_b01 = next(item for item in result["tasks"] if item["task_id"] == "B-01")
        task_b02 = next(item for item in result["tasks"] if item["task_id"] == "B-02")
        self.assertEqual(task_b01["status"], "INCOMPLETE")
        self.assertEqual(task_b02["status"], "INCOMPLETE")
        task_b06 = next(item for item in result["tasks"] if item["task_id"] == "B-06")
        self.assertEqual(task_b06["status"], "INCOMPLETE")
        self.assertTrue(any(item["name"] == "target-backfill" for item in task_b06["blockers"]))

    def test_task_is_blocked_when_only_an_upstream_task_is_incomplete(self):
        matrix = self._matrix(status="PASSED")
        matrix["gates"]["A"]["status"] = "INCOMPLETE"
        result = build_from_matrix(self.repo_root, matrix)
        task_b01 = next(item for item in result["tasks"] if item["task_id"] == "B-01")
        self.assertEqual(task_b01["status"], "BLOCKED")
        self.assertTrue(any(
            item["name"] == "upstream_gate_dependencies"
            for item in task_b01["blockers"]
        ))


if __name__ == "__main__":
    unittest.main()
