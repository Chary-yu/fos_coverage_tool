import json
import os
import tempfile
import unittest

from scripts.diagnostics.runtime_legacy_dependency_audit import audit as audit_legacy
from scripts.diagnostics.runtime_participation_audit import audit as audit_participation
from scripts.diagnostics.active_runtime_audit import audit as audit_active_runtime
from scripts.diagnostics.configured_runtime_audit import audit as audit_configured_runtime
from scripts.diagnostics.frontend_vnext_api_contract_audit import audit as audit_frontend
from scripts.diagnostics.scan_immutability_audit import audit as audit_immutability
from scripts.diagnostics.legacy_retirement_audit import audit as audit_legacy_retirement
from scripts.diagnostics.changed_test_selection import DiffResolutionError, changed_files, select
from scripts.diagnostics.performance_evidence_audit import audit as audit_performance


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

    def test_configured_runtime_and_active_evidence_have_distinct_contracts(self):
        configured = audit_configured_runtime(os.getcwd())
        self.assertEqual(configured["status"], "PASSED")
        active = audit_active_runtime(os.getcwd())
        self.assertEqual(active["evidence_class"], "active_runtime_audit")
        self.assertEqual(active["status"], "PARTIAL")
        self.assertIn("process_or_service", active["missing_evidence"])
        retirement = audit_legacy_retirement(os.getcwd())
        self.assertEqual(retirement["gate_status"], "INCOMPLETE")
        self.assertEqual(retirement["legacy_implementation_status"], "TRANSITIONAL_LEGACY")

    def test_changed_test_selection_fails_closed_when_revision_is_unavailable(self):
        with self.assertRaises(DiffResolutionError):
            changed_files(os.getcwd(), head="revision-does-not-exist")
        self.assertIn("tests.vnext.test_vnext_runtime", select([]))

    def test_cross_layer_performance_gate_rejects_browser_only_evidence(self):
        payload = {
            "coverage_virtual_scroll_100k": {
                "status": "PASSED", "request_count": 1, "response_bytes": 1,
                "max_response_bytes": 1, "time_to_first_visible_ms": 1,
                "time_to_target_line_ms": 1, "logical_line_count": 100000,
                "resident_js_lines": 1, "resident_js_lines_peak": 1,
                "dom_line_count": 1,
                "telemetry": {
                    "api_requests": 1, "network_chunks": 1,
                    "network_lines": 1, "max_dom_lines": 1,
                },
            }
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as stream:
            json.dump(payload, stream)
            stream.flush()
            partial = audit_performance(stream.name, allow_partial=True)
            strict = audit_performance(stream.name, require_cross_layer=True)
        self.assertEqual(partial["status"], "PARTIAL")
        self.assertEqual(strict["status"], "FAILED")
        self.assertIn("peak_rss_bytes", strict["missing_cross_layer_metrics"])
        self.assertEqual(audit_frontend(os.getcwd())["status"], "PASSED")
        self.assertEqual(audit_immutability()["status"], "PASSED")


if __name__ == "__main__":
    unittest.main()
