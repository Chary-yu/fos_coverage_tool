import os
import argparse
import json
import subprocess
import sys
import tempfile
import unittest

from scripts.diagnostics.acceptance_window_audit import _parse_time, audit as audit_window
from scripts.diagnostics.build_gate_evidence import (
    GATE_DIRECTORIES, GATE_FILES, build as build_gate_evidence,
    _manifest_artifact_attributes,
)
from scripts.diagnostics.production_inventory import collect_inventory, required_free_bytes
from scripts.diagnostics.skill_drift_audit import REQUIRED_FIELDS, REQUIRED_SKILLS, audit as audit_skills
from scripts.upgrade.evidence_manifest import EvidenceManifestV2


class ReleaseGovernanceToolsTest(unittest.TestCase):
    def test_disk_formula_includes_twenty_percent_or_ten_gib_margin(self):
        result = required_free_bytes(100, 200, 300, 400, 500, 600)
        self.assertEqual(result["preceding_sum"], 2100)
        self.assertEqual(result["safety_margin_bytes"], 10 * (1024 ** 3))
        self.assertEqual(result["required_free_bytes"], 10 * (1024 ** 3) + 2100)
        with self.assertRaises(ValueError):
            required_free_bytes(-1, 0, 0, 0, 0, 0)

    def test_production_inventory_fails_closed_without_live_release_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            current = os.path.join(directory, "current")
            candidate = os.path.join(directory, "candidate")
            backup = os.path.join(directory, "backups")
            for path in (current, candidate, backup):
                os.makedirs(path)
            current_config = os.path.join(current, "coverage_config.json")
            candidate_config = os.path.join(candidate, "coverage_config.json")
            payload = {
                "mysql": {"host": "127.0.0.1", "port": 3306,
                          "user": "coverage", "password": "secret",
                          "database": "coverage"},
                "server": {"host": "127.0.0.1", "port": 9528},
                "auth": {"mode": "reverse_proxy", "user_header": "X-Remote-User",
                         "trusted_proxy_addresses": ["127.0.0.1"]},
                "runtime_state": {"root": ".runtime-state", "jobs_dir": "jobs"},
                "runtime_mode": "vnext", "schema_version": 1,
            }
            for path in (current_config, candidate_config):
                with open(path, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream)
            args = argparse.Namespace(
                current_root=current, candidate_root=candidate,
                disk_root=directory, current_config=current_config,
                candidate_config=candidate_config, current_repository_root=None,
                candidate_repository_root=None, repository_root=[], service=[],
                process_pattern=[], port=[], persistent_root=[], jobs_root=[],
                backup_root=backup, proxy_config=[],
                current_release_bytes=1, candidate_release_bytes=1,
                final_target_db_estimate=1, verified_backup_bytes=1,
                max_temp_worktree_bytes=1, migration_temp_bytes=1,
            )
            result = collect_inventory(args)
            self.assertIn(result["status"], ("INCOMPLETE", "FAILED"))
            self.assertIn("service_process_inventory", result["completeness"]["missing"])
            self.assertIn("current_db_identity", result["completeness"]["missing"])
            self.assertNotEqual(result["exit_code"], 0)
            encoded = json.dumps(result["configs"], ensure_ascii=False)
            self.assertNotIn("secret", encoded)

    def test_production_inventory_proxy_boundary_requires_explicit_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            proxy = os.path.join(directory, "coverage.conf")
            with open(proxy, "w", encoding="utf-8") as stream:
                stream.write("proxy_pass http://127.0.0.1:19528;\n")
            from scripts.diagnostics.production_inventory import _proxy_observation
            result = _proxy_observation([proxy])
            self.assertEqual(result["status"], "INCOMPLETE")
            self.assertFalse(result["signals"]["remote_user_header"])

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

    def test_acceptance_window_rejects_malformed_counters_without_crashing(self):
        result = audit_window({
            "window_started_at": "2026-08-18T00:00:00Z",
            "window_ends_at": "2026-08-20T00:00:00Z",
            "now": "2026-08-21T00:00:00Z",
            "restart_recovery_passes": "not-a-number",
            "large_file_checks": -1,
        })
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertTrue(any("restart_recovery_passes" in item for item in result["violations"]))
        self.assertTrue(any("large_file_checks" in item for item in result["violations"]))

    def test_acceptance_window_cli_is_executable_from_repository_root(self):
        with tempfile.TemporaryDirectory() as root:
            input_path = os.path.join(root, "window.json")
            with open(input_path, "w", encoding="utf-8") as stream:
                json.dump({}, stream)
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/diagnostics/acceptance_window_audit.py",
                    "--input", input_path,
                ],
                cwd=os.getcwd(), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, universal_newlines=True,
            )
        self.assertEqual(completed.returncode, 1)
        self.assertIn('"status": "INCOMPLETE"', completed.stdout)
        self.assertNotIn("ModuleNotFoundError", completed.stderr)

    def test_acceptance_window_cli_reports_invalid_json_as_incomplete(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as stream:
            stream.write("not-json")
            stream.flush()
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/diagnostics/acceptance_window_audit.py",
                    "--input", stream.name,
                ],
                cwd=os.getcwd(), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, universal_newlines=True,
            )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("input could not be read", completed.stdout)
        self.assertEqual(completed.stderr, "")

    def test_skill_drift_cli_is_executable_from_repository_root(self):
        with tempfile.TemporaryDirectory() as root:
            input_path = os.path.join(root, "skills.json")
            with open(input_path, "w", encoding="utf-8") as stream:
                json.dump({}, stream)
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/diagnostics/skill_drift_audit.py",
                    "--input", input_path,
                ],
                cwd=os.getcwd(), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, universal_newlines=True,
            )
        self.assertEqual(completed.returncode, 1)
        self.assertIn('"status": "INCOMPLETE"', completed.stdout)
        self.assertNotIn("ModuleNotFoundError", completed.stderr)

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
                manifest = EvidenceManifestV2(
                    os.getcwd(), "gate-{}".format(gate.lower()),
                    manifest_path=os.path.join(gate_dir, "evidence-manifest-v2.json"),
                )
                valid, errors = manifest.validate()
                self.assertTrue(valid, "invalid Gate {} manifest: {}".format(gate, errors))

    def test_manifest_artifact_attributes_preserve_status_and_synthetic_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            passed = os.path.join(directory, "passed.json")
            with open(passed, "w", encoding="utf-8") as stream:
                json.dump({"status": "PASSED", "synthetic": False}, stream)
            status, synthetic, observed = _manifest_artifact_attributes(
                "final_source_review.json", passed, {"status": "PASSED"}
            )
            self.assertEqual(status, "PASSED")
            self.assertFalse(synthetic)
            self.assertEqual(observed, "PASSED")

            fixture = os.path.join(directory, "fixture.json")
            with open(fixture, "w", encoding="utf-8") as stream:
                json.dump({"status": "PASSED", "synthetic": True}, stream)
            status, synthetic, observed = _manifest_artifact_attributes(
                "performance_metrics.json", fixture, {"status": "PASSED"}
            )
            self.assertEqual(status, "INCOMPLETE")
            self.assertTrue(synthetic)
            self.assertEqual(observed, "PASSED")


if __name__ == "__main__":
    unittest.main()
