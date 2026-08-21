import os
import tempfile
import unittest

from scripts.diagnostics.acceptance_window_audit import _parse_time, audit as audit_window
from scripts.diagnostics.build_gate_evidence import (
    GATE_DIRECTORIES, GATE_FILES, build as build_gate_evidence,
)
from scripts.diagnostics.production_inventory import required_free_bytes
from scripts.diagnostics.skill_drift_audit import REQUIRED_FIELDS, REQUIRED_SKILLS, audit as audit_skills


class ReleaseGovernanceToolsTest(unittest.TestCase):
    def test_disk_formula_includes_twenty_percent_or_ten_gib_margin(self):
        result = required_free_bytes(100, 200, 300, 400, 500, 600)
        self.assertEqual(result["preceding_sum"], 2100)
        self.assertEqual(result["safety_margin_bytes"], 10 * (1024 ** 3))
        self.assertEqual(result["required_free_bytes"], 10 * (1024 ** 3) + 2100)
        with self.assertRaises(ValueError):
            required_free_bytes(-1, 0, 0, 0, 0, 0)

    def test_acceptance_window_fails_closed_without_exit_conditions(self):
        result = audit_window({
            "window_started_at": "2026-08-18T00:00:00Z",
            "window_ends_at": "2026-08-20T00:00:00Z",
            "now": "2026-08-21T00:00:00Z",
        })
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertTrue(result["violations"])

    def test_acceptance_window_passes_only_with_all_conditions(self):
        result = audit_window({
            "window_started_at": "2026-08-18T00:00:00Z",
            "window_ends_at": "2026-08-20T00:00:00Z",
            "now": "2026-08-21T00:00:00Z",
            "successful_scans": [
                {"inheritance": True}, {"ordinary_pending": True},
                {"inheritance": False},
            ],
            "restart_recovery_passes": 1,
            "large_file_checks": 1,
            "technical_failure_trend": "stable",
            "db_integrity_failures": 0,
            "semantic_hash_failures": 0,
        })
        self.assertEqual(result["status"], "PASSED")

    def test_acceptance_window_normalizes_explicit_timezone_offsets(self):
        self.assertEqual(
            _parse_time("2026-08-21T08:00:00+08:00"),
            _parse_time("2026-08-21T00:00:00Z"),
        )

    def test_skill_drift_requires_every_owner_field(self):
        incomplete = audit_skills({"candidate_revision": "abc", "skills": {}})
        self.assertEqual(incomplete["status"], "INCOMPLETE")
        complete = {
            "candidate_revision": "abc",
            "skills": {
                name: {field: "verified" for field in REQUIRED_FIELDS}
                for name in REQUIRED_SKILLS
            },
        }
        self.assertEqual(audit_skills(complete, "abc")["status"], "PASSED")
        self.assertEqual(audit_skills(complete, "def")["status"], "INCOMPLETE")

    def test_gate_evidence_bundle_has_exact_artifact_names_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            result = build_gate_evidence(
                ".", directory, run_tests=False
            )
            self.assertEqual(result["status"], "INCOMPLETE")
            self.assertEqual(set(result["gates"]), set("ABCDEF"))
            for gate, names in GATE_FILES.items():
                gate_dir = os.path.join(directory, "gate-{}".format(gate.lower()))
                for name in names:
                    self.assertTrue(
                        os.path.isfile(os.path.join(gate_dir, name)),
                        "missing {} for Gate {}".format(name, gate),
                    )
                for name in GATE_DIRECTORIES.get(gate, ()):
                    self.assertTrue(os.path.isdir(os.path.join(gate_dir, name)))
                if gate == "F":
                    self.assertTrue(os.path.isfile(os.path.join(
                        gate_dir, "fresh_inventory", "summary.json"
                    )))
                self.assertTrue(os.path.isfile(os.path.join(
                    gate_dir, "evidence-manifest-v2.json"
                )))


if __name__ == "__main__":
    unittest.main()
