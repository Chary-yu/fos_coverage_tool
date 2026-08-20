import os
import unittest

from scripts.diagnostics.runtime_legacy_dependency_audit import audit as audit_legacy
from scripts.diagnostics.runtime_participation_audit import audit as audit_participation


class ArchitectureAuditTest(unittest.TestCase):
    def test_legacy_audit_reports_transitional_implementations_explicitly(self):
        result = audit_legacy(os.getcwd())
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["legacy_implementation_status"], "TRANSITIONAL_LEGACY")
        transitional = {
            item["path"] for item in result["classification"]["TRANSITIONAL_LEGACY"]
        }
        self.assertEqual(
            transitional,
            {"app/legacy_runtime.py", "app/incremental/legacy.py"},
        )
        self.assertEqual(result["classification"]["RETIRED"], [])

    def test_path_index_participation_audit_checks_vnext_orchestrator(self):
        result = audit_participation()
        self.assertEqual(result["status"], "PASSED")
        check = next(item for item in result["checks"] if item["name"] == "lcov_path_index")
        self.assertIn("app/incremental/orchestrator.py", check["paths"])


if __name__ == "__main__":
    unittest.main()
