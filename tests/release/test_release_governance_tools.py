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
from scripts.diagnostics.production_inventory import (
    collect_inventory, required_free_bytes, write_evidence_manifest,
)
from scripts.diagnostics.skill_drift_audit import REQUIRED_FIELDS, REQUIRED_SKILLS, audit as audit_skills
from scripts.upgrade.evidence_manifest import EvidenceManifestV2


class ReleaseGovernanceToolsTest(unittest.TestCase):
    def test_ci_has_manual_exact_sha_candidate_browser_lane(self):
        with open(
                os.path.join(os.getcwd(), ".github", "workflows", "ci.yml"),
                encoding="utf-8") as stream:
            workflow = stream.read()
        self.assertIn("real_browser_url:", workflow)
        self.assertIn("real_browser_expected_revision:", workflow)
        self.assertIn("real-browser-candidate:", workflow)
        self.assertIn("real_browser_evidence.js", workflow)
        self.assertIn("--browser-evidence-output", workflow)
        self.assertIn("--evidence-output", workflow)
        self.assertIn("gate_task_status.py", workflow)
        self.assertIn("gate-task-status.json", workflow)
        self.assertIn("dod_status.py", workflow)
        self.assertIn("gate-dod-status.json", workflow)
        self.assertIn("release_readiness.py", workflow)
        self.assertIn("release-readiness.json", workflow)
        self.assertIn("actions/upload-artifact@65462800fd760344b1a7b4382951275a0abb4808", workflow)
        self.assertIn("fetch-depth: 0", workflow)

    def test_ci_has_required_candidate_release_lanes(self):
        with open(
                os.path.join(os.getcwd(), ".github", "workflows", "ci.yml"),
                encoding="utf-8") as stream:
            workflow = stream.read()
        self.assertIn("candidate-source-gate:", workflow)
        self.assertIn("Candidate source gate (required source lanes)", workflow)
        self.assertNotIn("candidate-release-gate:", workflow)
        for lane in (
                "semantic-migration-regression",
                "mysql55-compatibility",
                "py36-compat",
                "specialist-regression"):
            self.assertIn("- {}".format(lane), workflow)
        with open("requirements-py36.txt", encoding="utf-8") as stream:
            requirements = stream.read()
        self.assertIn("PyMySQL==0.10.1", requirements)
        self.assertIn(
            "python -m pip install --no-cache-dir -r requirements-py36.txt",
            workflow,
        )
        self.assertIn(
            "pymysql.__version__ == '0.10.1'", workflow,
        )
        self.assertIn("Run MariaDB 5.5 rehearsal under Python 3.6", workflow)
        self.assertIn("mariadb55_py36_compatibility.json", workflow)
        self.assertIn("evidence['python_runtime'].startswith('3.6')", workflow)
        self.assertIn(
            "migration_ready = migration['checks']['file_state_ready_gate']",
            workflow,
        )
        self.assertIn(
            "2> .artifacts/mariadb55_py36_compatibility.stderr | tee "
            ".artifacts/mariadb55_py36_compatibility.json",
            workflow,
        )
        self.assertNotIn(
            "bash -s <<'PY' 2>&1 | tee "
            ".artifacts/mariadb55_py36_compatibility.json",
            workflow,
        )
        self.assertIn("file_state = evidence['checks']['file_state_ready_gate']", workflow)
        self.assertIn("for project in file_state[run]['projects'].values()", workflow)
        self.assertIn(
            "--manifest-output .artifacts/verified-production-mariadb55/evidence-manifest-v2.json",
            workflow,
        )
        self.assertIn(
            "manifest['evidence_schema_version'] == 2", workflow,
        )
        for test_module in (
                "tests.vnext.test_reliability_repairs",
                "tests.release.test_gate_matrix",
                "tests.release.test_immutable_release_publication",
                "tests.release.test_legacy_background_serialization",
                "tests.release.test_validation_session",
                "tests.release.test_verified_backup_rehearsal"):
            self.assertIn(test_module, workflow)
        for evidence_field in (
                "evidence['checks']['file_state_ready_gate']",
                "evidence['checks']['file_state_paged_reads']",
                "evidence['checks']['transaction_rollback']"):
            self.assertIn(evidence_field, workflow)
        self.assertEqual(workflow.count("docker run --rm -i"), 2)
        self.assertIn(".artifacts/py36-runtime-evidence.json", workflow)
        self.assertIn("test -s .artifacts/py36-runtime-evidence.json", workflow)
        self.assertIn("grep -q '\"tests_executed\": true' .artifacts/py36-runtime-evidence.json", workflow)
        self.assertIn("grep -q '\"pymysql\": \"0.10.1\"' .artifacts/py36-runtime-evidence.json", workflow)
        self.assertIn("assert int(aggregate['pending_total']) == (", workflow)
        self.assertIn("int(aggregate['ordinary_pending_total'])", workflow)
        self.assertIn("int(aggregate['inherited_pending_total'])", workflow)
        self.assertIn("int(aggregate['manual_draft_pending_total'])", workflow)

    def test_ci_distinguishes_source_candidate_from_production_ready(self):
        with open(
                os.path.join(os.getcwd(), ".github", "workflows", "ci.yml"),
                encoding="utf-8") as stream:
            workflow = stream.read()
        self.assertIn("production-ready-gate:", workflow)
        self.assertIn("Production READY gate (manual external evidence)", workflow)
        self.assertIn("Production READY requires manual external evidence", workflow)
        for lane in (
                "verified-production-mariadb55",
                "real-browser-candidate",
                "cross-layer-performance"):
            self.assertIn("- {}".format(lane), workflow)
        self.assertIn("github.event_name == 'workflow_dispatch'", workflow)

    def test_perf_benchmark_help_is_side_effect_free(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "benchmark.json")
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/diagnostics/perf_benchmark.py",
                    "--help",
                    "--output",
                    output,
                ],
                cwd=os.getcwd(), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, universal_newlines=True,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertIn("--output", completed.stdout)
            self.assertFalse(os.path.exists(output))

    def test_file_state_rebuild_benchmark_help_is_side_effect_free(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "file-state.json")
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/diagnostics/file_state_rebuild_benchmark.py",
                    "--help", "--output", output,
                ],
                cwd=os.getcwd(), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, universal_newlines=True,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertIn("--output", completed.stdout)
            self.assertFalse(os.path.exists(output))

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

    def test_production_inventory_can_emit_gate_f_manifest_v2(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = os.path.join(directory, "fresh-inventory.json")
            manifest_path = os.path.join(directory, "evidence-manifest-v2.json")
            with open(artifact, "w", encoding="utf-8") as stream:
                json.dump({"status": "INCOMPLETE"}, stream)
            from app.release_identity import generate_release_identity
            result = {
                "status": "INCOMPLETE", "exit_code": 1,
                "release_identity": generate_release_identity(os.getcwd()),
                "host_identity": {"hostname": "test"},
                "started_at": "2026-08-21T00:00:00Z",
                "finished_at": "2026-08-21T00:00:01Z",
                "command_or_action": "production inventory test",
                "database_runtime_identity": {},
                "completeness": {"missing": ["current_db_identity"]},
            }
            write_evidence_manifest(result, artifact, manifest_path)
            manifest = EvidenceManifestV2(
                os.getcwd(), "gate-f", manifest_path=manifest_path
            )
            valid, errors = manifest.validate()
            self.assertTrue(valid, errors)
            self.assertEqual(manifest.data["evidence"][0]["status"], "INCOMPLETE")
            self.assertFalse(manifest.data["evidence"][0]["synthetic"])

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
                    with open(
                            os.path.join(gate_dir, "legacy_retirement.json"),
                            encoding="utf-8") as stream:
                        legacy_retirement = json.load(stream)
                    self.assertEqual(
                        legacy_retirement["evidence_class"],
                        "legacy_retirement_gate",
                    )
                self.assertTrue(os.path.isfile(os.path.join(
                    gate_dir, "evidence-manifest-v2.json"
                )))
                manifest = EvidenceManifestV2(
                    os.getcwd(), "gate-{}".format(gate.lower()),
                    manifest_path=os.path.join(gate_dir, "evidence-manifest-v2.json"),
                )
                valid, errors = manifest.validate()
                self.assertTrue(valid, "invalid Gate {} manifest: {}".format(gate, errors))

    def test_gate_bundle_consumes_external_fresh_inventory_manifest(self):
        old = os.environ.get("COVERAGE_GATE_F_INVENTORY_EVIDENCE")
        try:
            with tempfile.TemporaryDirectory() as directory:
                raw = os.path.join(directory, "fresh-inventory.json")
                external_manifest = os.path.join(directory, "evidence-manifest-v2.json")
                with open(raw, "w", encoding="utf-8") as stream:
                    json.dump({
                        "status": "INCOMPLETE",
                        "evidence_class": "fresh_production_inventory",
                        "synthetic": False,
                        "violations": ["external test fixture is intentionally incomplete"],
                    }, stream)
                from app.release_identity import generate_release_identity
                write_evidence_manifest({
                    "status": "INCOMPLETE", "exit_code": 1,
                    "release_identity": generate_release_identity(os.getcwd()),
                    "host_identity": {"hostname": "test"},
                    "started_at": "2026-08-21T00:00:00Z",
                    "finished_at": "2026-08-21T00:00:01Z",
                    "command_or_action": "external inventory fixture",
                    "database_runtime_identity": {},
                }, raw, external_manifest)
                os.environ["COVERAGE_GATE_F_INVENTORY_EVIDENCE"] = external_manifest
                output_root = os.path.join(directory, "bundle")
                result = build_gate_evidence(".", output_root, run_tests=False)
                with open(os.path.join(
                        output_root, "gate-f", "fresh_inventory", "summary.json"),
                        encoding="utf-8") as stream:
                    summary = json.load(stream)
                self.assertEqual(summary["status"], "INCOMPLETE")
                with open(os.path.join(
                        output_root, "gate-f", "candidate_layout.json"),
                        encoding="utf-8") as stream:
                    layout = json.load(stream)
                self.assertEqual(layout["status"], "INCOMPLETE")
                manifest = EvidenceManifestV2(
                    os.getcwd(), "gate-f", manifest_path=os.path.join(
                        output_root, "gate-f", "evidence-manifest-v2.json"
                    )
                )
                records = [
                    item for item in manifest.data["evidence"]
                    if item.get("evidence_id") == "f-fresh_inventory-summary-json"
                ]
                self.assertEqual(len(records), 1)
                self.assertTrue(records[0]["source_inputs_sha256"])
        finally:
            if old is None:
                os.environ.pop("COVERAGE_GATE_F_INVENTORY_EVIDENCE", None)
            else:
                os.environ["COVERAGE_GATE_F_INVENTORY_EVIDENCE"] = old

    def test_gate_bundle_consumes_external_gate_a_and_c_artifacts(self):
        names = {
            "COVERAGE_GATE_A_MARIADB_EVIDENCE": "mariadb55_preflight.json",
            "COVERAGE_GATE_C_RESTART_EVIDENCE": "checkpoint_resume_tests.json",
        }
        old = {name: os.environ.get(name) for name in names}
        try:
            with tempfile.TemporaryDirectory() as directory:
                for env_name in names:
                    path = os.path.join(directory, env_name + ".json")
                    with open(path, "w", encoding="utf-8") as stream:
                        json.dump({
                            "status": "INCOMPLETE",
                            "evidence_class": "external_fixture",
                            "synthetic": False,
                            "marker": env_name,
                        }, stream)
                    os.environ[env_name] = path
                output_root = os.path.join(directory, "bundle")
                build_gate_evidence(".", output_root, run_tests=False)
                for env_name, artifact_name in names.items():
                    gate = "a" if "A_" in env_name else "c"
                    artifact = os.path.join(output_root, "gate-" + gate, artifact_name)
                    with open(artifact, encoding="utf-8") as stream:
                        payload = json.load(stream)
                    self.assertEqual(payload["marker"], env_name)
                    manifest = EvidenceManifestV2(
                        os.getcwd(), "gate-" + gate,
                        manifest_path=os.path.join(
                            output_root, "gate-" + gate,
                            "evidence-manifest-v2.json",
                        ),
                    )
                    record_id = "{}-{}".format(
                        gate, artifact_name.replace("/", "-").replace(".", "-")
                    )
                    record = next(
                        item for item in manifest.data["evidence"]
                        if item["evidence_id"] == record_id
                    )
                    self.assertTrue(record["source_inputs_sha256"])
        finally:
            for env_name, value in old.items():
                if value is None:
                    os.environ.pop(env_name, None)
                else:
                    os.environ[env_name] = value

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
