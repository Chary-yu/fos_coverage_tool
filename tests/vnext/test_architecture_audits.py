import json
import os
import tempfile
import unittest

from scripts.diagnostics.runtime_legacy_dependency_audit import audit as audit_legacy
from scripts.diagnostics.runtime_participation_audit import audit as audit_participation
from scripts.diagnostics.active_runtime_audit import (
    _bound_config,
    _release_matches_revision,
    audit as audit_active_runtime,
)
from scripts.diagnostics.configured_runtime_audit import audit as audit_configured_runtime
from scripts.diagnostics.frontend_vnext_api_contract_audit import audit as audit_frontend
from scripts.diagnostics.scan_immutability_audit import audit as audit_immutability
from scripts.diagnostics.legacy_retirement_audit import audit as audit_legacy_retirement
from scripts.diagnostics.legacy_retirement_audit import main as legacy_retirement_main
from scripts.diagnostics.contract_artifact_audit import audit as audit_contract_artifacts
from scripts.diagnostics.task_manifest_audit import audit as audit_task_manifest
from scripts.diagnostics.changed_test_selection import DiffResolutionError, changed_files, select
from scripts.diagnostics.performance_evidence_audit import audit as audit_performance
from scripts.diagnostics.legacy_compatibility_smoke import audit as audit_legacy_compatibility


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
            {"app/compat/legacy_runtime_impl.py", "app/compat/incremental_impl.py"},
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
        self.assertTrue(retirement["retirement_checks"]["no_legacy_deployment"])
        self.assertFalse(retirement["retirement_checks"]["legacy_usage_zero_for_window"])

    def test_legacy_retirement_rejects_failed_compatibility_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = os.path.join(directory, "legacy-compatibility.json")
            with open(manifest, "w", encoding="utf-8") as stream:
                json.dump({"status": "FAILED", "candidate_revision": "abc"}, stream)
            result = audit_legacy_retirement(
                os.getcwd(), compatibility_manifest=manifest,
            )
        self.assertFalse(result["retirement_checks"]["compatibility_tests"])
        self.assertIn("status=PASSED", result["compatibility_evidence"]["missing_keys"])

    def test_legacy_retirement_rejects_stale_compatibility_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = os.path.join(directory, "legacy-compatibility.json")
            with open(manifest, "w", encoding="utf-8") as stream:
                json.dump({"status": "PASSED", "candidate_revision": "stale"}, stream)
            result = audit_legacy_retirement(
                os.getcwd(), compatibility_manifest=manifest,
            )
        self.assertFalse(result["retirement_checks"]["compatibility_tests"])
        self.assertTrue(any(
            item.startswith("candidate_revision=")
            for item in result["compatibility_evidence"]["missing_keys"]
        ))

    def test_legacy_retirement_cli_writes_exact_sha_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "legacy-retirement.json")
            self.assertEqual(legacy_retirement_main(["--output", output]), 0)
            with open(output, "r", encoding="utf-8") as stream:
                result = json.load(stream)
        self.assertEqual(result["candidate_revision"],
                         audit_legacy_retirement(os.getcwd())["candidate_revision"])
        self.assertEqual(result["evidence_class"], "legacy_retirement_gate")
        self.assertEqual(result["gate_status"], "INCOMPLETE")
        self.assertEqual(result["legacy_implementation_status"], "TRANSITIONAL_LEGACY")

    def test_legacy_compatibility_smoke_exercises_import_and_cli_surfaces(self):
        result = audit_legacy_compatibility(os.getcwd())
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(
            {item["surface"] for item in result["surfaces"]},
            {
                "enhance_coverage", "coverage_check", "code_detail_service",
                "code_region", "source_reader",
            },
        )
        self.assertEqual(
            {item["surface"] for item in result["cli_surfaces"]},
            {"enhance_coverage.py", "coverage_check.py"},
        )
        self.assertTrue(all(item["exit_code"] == 0 for item in result["cli_surfaces"]))

    def test_active_runtime_binding_uses_process_candidate_config(self):
        process = {
            "available": True,
            "repo_root": os.getcwd(),
            "cmdline": ["python", "enhance_coverage.py", "server", "--config",
                         "config/coverage_config.staging.example.json"],
            "environment": {},
        }
        result = _bound_config(process)
        self.assertEqual(
            result["selected_realpath"],
            os.path.realpath(os.path.join(
                os.getcwd(), "config/coverage_config.staging.example.json"
            )),
        )
        self.assertTrue(result["matches_requested"])

    def test_active_runtime_requires_exact_release_revision(self):
        release = {"commit_sha": "candidate-sha", "build_id": "candidate-build"}
        self.assertTrue(_release_matches_revision(release, "candidate-sha"))
        self.assertFalse(_release_matches_revision(release, "different-sha"))
        self.assertFalse(_release_matches_revision(release, ""))

    def test_changed_test_selection_fails_closed_when_revision_is_unavailable(self):
        with self.assertRaises(DiffResolutionError):
            changed_files(os.getcwd(), head="revision-does-not-exist")
        self.assertIn("tests.vnext.test_vnext_runtime", select([]))

    def test_changed_test_selection_runs_a_modified_test_module_directly(self):
        selected = select(["tests/vnext/test_scan_import_lifecycle.py"])
        self.assertIn("tests.vnext.test_scan_import_lifecycle", selected)

    def test_changed_test_selection_covers_inheritance_owners(self):
        inheritance = select(["app/inheritance/engine.py"])
        self.assertIn("tests.vnext.test_inheritance_engine", inheritance)
        self.assertIn("tests.vnext.test_deterministic_inheritance_corpus", inheritance)
        review = select(["app/services/inheritance_review_service.py"])
        self.assertIn("tests.vnext.test_api_export_security", review)

    def test_changed_test_selection_covers_compatibility_telemetry(self):
        selected = select(["app/compat/telemetry.py"])
        self.assertIn("tests.vnext.test_legacy_telemetry", selected)
        self.assertIn("tests.vnext.test_runtime_config", selected)

    def test_changed_test_selection_covers_backup_ownership(self):
        upgrade = select(["scripts/upgrade/run_verified_backup_rehearsal.py"])
        self.assertIn("tests.release.test_verified_backup_rehearsal", upgrade)
        maintenance = select(["scripts/maintenance/mysql_backup.py"])
        self.assertIn("tests.database.test_phase0_baseline", maintenance)
        self.assertIn("tests.release.test_verified_backup_rehearsal", maintenance)

    def test_changed_test_selection_covers_definition_of_done_governance(self):
        selected = select(["scripts/diagnostics/dod_status.py"])
        self.assertIn("tests.release.test_dod_status", selected)
        self.assertIn("tests.release.test_release_readiness", selected)
        selected_manifest = select(["docs/gate_dod_manifest.json"])
        self.assertIn("tests.release.test_dod_status", selected_manifest)

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
        self.assertFalse(partial["release_eligible"])
        self.assertEqual(strict["status"], "FAILED")
        self.assertIn("peak_rss_bytes", strict["missing_cross_layer_metrics"])
        with tempfile.TemporaryDirectory() as directory:
            missing = os.path.join(directory, "missing.json")
            result = audit_performance(missing, allow_partial=True)
            self.assertEqual(result["status"], "FAILED")
            self.assertEqual(result["browser_status"], "FAILED")
        self.assertEqual(audit_frontend(os.getcwd())["status"], "PASSED")
        self.assertEqual(audit_contract_artifacts(os.getcwd())["status"], "PASSED")
        task_manifest = audit_task_manifest(os.getcwd())
        self.assertEqual(task_manifest["status"], "PASSED")
        self.assertEqual(task_manifest["expected_task_count"], 80)
        self.assertEqual(audit_immutability()["status"], "PASSED")


if __name__ == "__main__":
    unittest.main()
