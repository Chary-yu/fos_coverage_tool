import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts.release.local_compatibility_gate import (
    MARIADB55_IMAGE, PYTHON36_IMAGE, run_gate,
)


class LocalCompatibilityGateTest(unittest.TestCase):
    def test_images_are_digest_pinned_and_not_floating(self):
        for image in (PYTHON36_IMAGE, MARIADB55_IMAGE):
            self.assertIn("@sha256:", image)
            self.assertNotIn(":latest", image)

    @staticmethod
    def _surface(python_status="NOT_PRODUCTION_PYTHON",
                 mariadb_status="NOT_DB_SENSITIVE"):
        return {
            "schema_version": 1,
            "source_commit_sha": "a" * 40,
            "baseline_commit_sha": "b" * 40,
            "changed_paths": [],
            "python": {
                "status": python_status,
                "changed_paths": [],
                "production_execution": python_status ==
                "PRODUCTION_PYTHON_CHANGED",
            },
            "mariadb": {
                "status": mariadb_status,
                "changed_paths": [],
                "production_database_execution": mariadb_status ==
                "DB_SENSITIVE_CHANGED",
            },
        }

    def test_no_db_surface_is_not_applicable_without_docker(self):
        with tempfile.TemporaryDirectory(prefix="compatibility-gate-") as root:
            output = os.path.join(root, "compatibility_tests.json")
            with mock.patch(
                    "scripts.release.local_compatibility_gate.classify_compatibility_surface",
                    return_value=self._surface()), mock.patch(
                    "scripts.release.local_compatibility_gate.shutil.which",
                    return_value=None):
                result = run_gate(os.getcwd(), output, "a" * 40, "b" * 40)
            self.assertEqual(result["status"], "NOT_APPLICABLE")
            self.assertEqual(result["PYTHON36_COMPAT"], "NOT_APPLICABLE")
            self.assertEqual(result["MARIADB55_COMPAT"], "NOT_APPLICABLE")
            self.assertTrue(result["isolated_candidate_staging_allowed"])
            self.assertTrue(result["publication_allowed"])
            self.assertTrue(result["cutover_allowed"])
            with open(output, encoding="utf-8") as stream:
                self.assertEqual(json.load(stream)["production_mutation"], "NONE")

    def test_changed_production_python_without_local_36_is_deferred(self):
        with tempfile.TemporaryDirectory(prefix="compatibility-gate-") as root:
            output = os.path.join(root, "compatibility_tests.json")
            surface = self._surface(python_status="PRODUCTION_PYTHON_CHANGED")
            with mock.patch(
                    "scripts.release.local_compatibility_gate.classify_compatibility_surface",
                    return_value=surface), mock.patch(
                    "scripts.release.local_compatibility_gate.shutil.which",
                    return_value=None):
                result = run_gate(os.getcwd(), output, "a" * 40, "b" * 40)
            self.assertEqual(result["status"], "DEFERRED_TO_TARGET_RUNTIME")
            self.assertEqual(
                result["PYTHON36_COMPAT"], "DEFERRED_TO_TARGET_RUNTIME"
            )
            self.assertEqual(result["MARIADB55_COMPAT"], "NOT_APPLICABLE")
            self.assertTrue(result["isolated_candidate_staging_allowed"])
            self.assertFalse(result["publication_allowed"])
            self.assertFalse(result["cutover_allowed"])

    def test_applicable_runtime_failure_is_fail_not_deferred(self):
        with tempfile.TemporaryDirectory(prefix="compatibility-gate-") as root:
            output = os.path.join(root, "compatibility_tests.json")
            surface = self._surface(python_status="PRODUCTION_PYTHON_CHANGED")
            with mock.patch(
                    "scripts.release.local_compatibility_gate.classify_compatibility_surface",
                    return_value=surface), mock.patch(
                    "scripts.release.local_compatibility_gate.shutil.which",
                    side_effect=lambda name: "/usr/bin/docker" if name == "docker" else None
                    ), mock.patch(
                    "scripts.release.local_compatibility_gate._run",
                    return_value=(0, "docker-ready")), mock.patch(
                    "scripts.release.local_compatibility_gate._python36_check",
                    return_value={"status": "FAIL", "compatibility": "FAIL"}):
                result = run_gate(os.getcwd(), output, "a" * 40, "b" * 40)
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["PYTHON36_COMPAT"], "FAIL")
            self.assertFalse(result["isolated_candidate_staging_allowed"])
            self.assertFalse(result["publication_allowed"])
            self.assertFalse(result["cutover_allowed"])

    def test_cli_runs_from_repository_root_without_package_path_setup(self):
        source = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=os.getcwd()
        ).decode("ascii").strip()
        with tempfile.TemporaryDirectory(prefix="compatibility-cli-") as root:
            output = os.path.join(root, "compatibility.json")
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/release/local_compatibility_gate.py",
                    "--repo-root", os.getcwd(),
                    "--source-sha", source,
                    "--baseline-sha", source,
                    "--output", output,
                ],
                cwd=os.getcwd(), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, universal_newlines=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            with open(output, encoding="utf-8") as stream:
                payload = json.load(stream)
        self.assertEqual(payload["status"], "NOT_APPLICABLE")


if __name__ == "__main__":
    unittest.main()
