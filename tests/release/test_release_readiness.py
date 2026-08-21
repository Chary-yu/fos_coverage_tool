import os
import unittest

from scripts.diagnostics.release_readiness import build
from scripts.diagnostics.dod_status import EXPECTED_DOD_IDS
from scripts.diagnostics.task_manifest_audit import EXPECTED_TASKS


class ReleaseReadinessTest(unittest.TestCase):
    def setUp(self):
        self.repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
        self.revision = __import__("subprocess").check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo_root
        ).decode("ascii").strip()

    def _inputs(self):
        matrix = {
            "candidate_revision": self.revision,
            "release_identity": {"commit_sha": self.revision},
            "gates": {
                gate: {"status": "PASSED", "missing_evidence": []}
                for gate in "ABCDEF"
            },
        }
        tasks = {"status": "PASSED", "candidate_revision": self.revision,
                 "task_count": len(EXPECTED_TASKS),
                 "tasks": [{"task_id": task_id, "status": "PASSED"}
                           for task_id in EXPECTED_TASKS]}
        dod = {"status": "PASSED", "candidate_revision": self.revision,
               "dod_count": len(EXPECTED_DOD_IDS),
               "items": [{"dod_id": dod_id, "status": "PASSED"}
                         for dod_id in EXPECTED_DOD_IDS]}
        risk = {"candidate_revision": self.revision, "risks": []}
        return matrix, tasks, dod, risk

    def test_missing_gate_or_risk_provenance_is_not_ready(self):
        matrix, tasks, dod, _ = self._inputs()
        result = build(
            self.repo_root, matrix=matrix, task_status=tasks,
            dod_status=dod, risk_register=None,
        )
        self.assertEqual(result["decision"], "NOT_READY")
        self.assertTrue(any(item["type"] == "risk_register" for item in result["blockers"]))

    def test_missing_dod_status_is_not_ready(self):
        matrix, tasks, _, risk = self._inputs()
        result = build(
            self.repo_root, matrix=matrix, task_status=tasks,
            dod_status=None, risk_register=risk,
        )
        self.assertEqual(result["decision"], "NOT_READY")
        self.assertTrue(any(item["type"] == "dod_status" for item in result["blockers"]))

    def test_all_gates_and_empty_risk_register_can_be_ready(self):
        matrix, tasks, dod, risk = self._inputs()
        result = build(
            self.repo_root, matrix=matrix, task_status=tasks,
            dod_status=dod, risk_register=risk,
        )
        self.assertEqual(result["decision"], "READY")
        self.assertEqual(result["blockers"], [])

    def test_approved_p2_is_accepted_but_p1_can_never_be_accepted(self):
        matrix, tasks, dod, risk = self._inputs()
        risk["risks"] = [{
            "id": "P2-PERF",
            "severity": "P2",
            "status": "APPROVED",
            "owner": "release",
            "approved_by": "owner",
            "approved_at": "2026-08-21T00:00:00Z",
            "evidence_ref": "evidence/p2-perf.json",
        }]
        result = build(
            self.repo_root, matrix=matrix, task_status=tasks,
            dod_status=dod, risk_register=risk,
        )
        self.assertEqual(result["decision"], "READY_WITH_ACCEPTED_RISK")
        risk["risks"][0]["severity"] = "P1"
        result = build(
            self.repo_root, matrix=matrix, task_status=tasks,
            dod_status=dod, risk_register=risk,
        )
        self.assertEqual(result["decision"], "NOT_READY")
        self.assertTrue(any("unresolved P0/P1" in item.get("reason", "") for item in result["blockers"]))

    def test_stale_matrix_revision_is_not_ready(self):
        matrix, tasks, dod, risk = self._inputs()
        matrix["candidate_revision"] = "0" * 40
        result = build(
            self.repo_root, matrix=matrix, task_status=tasks,
            dod_status=dod, risk_register=risk,
        )
        self.assertEqual(result["decision"], "NOT_READY")
        self.assertTrue(any(item["type"] == "provenance" for item in result["blockers"]))


if __name__ == "__main__":
    unittest.main()
