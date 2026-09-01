import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))


def _write_revision_artifact(path, revision, workload_hash, values,
                             evidence_class="release_performance_revision"):
    tiers = {}
    for name, value in values.items():
        tiers[name] = {"status": "PASSED", "measured_ms": value}
    payload = {
        "status": "PASSED",
        "evidence_class": evidence_class,
        "comparison_type": "single_revision",
        "revision": revision,
        "workload_id": "coverage-release-browser-v1",
        "workload_hash": workload_hash,
        "environment_identity": {
            "browser": "chromium-128",
            "os": "linux",
            "arch": "x86_64",
            "runner_class": "release-candidate",
        },
        "exit_code": 0,
        "tiers": tiers,
        "coverage_virtual_scroll_100k": {
            "status": "PASSED",
            "elapsed_ms": values["Tier_D_100k"],
            "logical_line_count": 100000,
            "resident_js_lines_peak": 8000,
            "dom_line_count": 634,
        },
    }
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2)


class TestReleasePerformanceAB(unittest.TestCase):
    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        self.script = os.path.join(ROOT, "scripts", "diagnostics", "release_performance_ab.js")

    def test_combines_two_exact_revision_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline = os.path.join(directory, "baseline.json")
            candidate = os.path.join(directory, "candidate.json")
            output = os.path.join(directory, "release-ab.json")
            workload_hash = "sha256:release-workload-v1"
            _write_revision_artifact(
                baseline, "before-commit", workload_hash,
                {"Tier_A_1k": 10, "Tier_B_10k": 20, "Tier_C_50k": 40, "Tier_D_100k": 80},
            )
            _write_revision_artifact(
                candidate, "candidate-commit", workload_hash,
                {"Tier_A_1k": 11, "Tier_B_10k": 21, "Tier_C_50k": 42, "Tier_D_100k": 84},
            )
            result = subprocess.run([
                "node", self.script,
                "--baseline-artifact", baseline,
                "--candidate-artifact", candidate,
                "--baseline-commit", "before-commit",
                "--candidate-commit", "candidate-commit",
                "--workload-hash", workload_hash,
                "--release-validation-session-id", "attempt-candidate",
                "--candidate-artifact-sha256", "a" * 64,
                "--served-root-sha256", "b" * 64,
                "--output", output,
            ], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
            with open(output, "r", encoding="utf-8") as stream:
                evidence = json.load(stream)
            self.assertEqual(evidence["evidence_class"], "release_performance_ab")
            self.assertEqual(evidence["candidate_commit"], "candidate-commit")
            self.assertEqual(evidence["release_validation_session_id"], "attempt-candidate")
            self.assertEqual(evidence["candidate_artifact_sha256"], "a" * 64)
            self.assertEqual(evidence["served_root_sha256"], "b" * 64)
            self.assertEqual(evidence["Tier_D_100k"]["status"], "PASSED")
            self.assertTrue(os.path.isfile(evidence["source_artifacts"]["baseline"]["path"]))
            with open(baseline, "rb") as baseline_stream:
                baseline_sha = hashlib.sha256(baseline_stream.read()).hexdigest()
            self.assertEqual(
                evidence["source_artifacts"]["baseline"]["sha256"],
                baseline_sha,
            )

    def test_same_run_synthetic_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline = os.path.join(directory, "baseline.json")
            candidate = os.path.join(directory, "candidate.json")
            output = os.path.join(directory, "release-ab.json")
            workload_hash = "sha256:release-workload-v1"
            _write_revision_artifact(
                baseline, "before-commit", workload_hash,
                {"Tier_A_1k": 10, "Tier_B_10k": 20, "Tier_C_50k": 40, "Tier_D_100k": 80},
                evidence_class="synthetic_dom_microbenchmark",
            )
            _write_revision_artifact(
                candidate, "candidate-commit", workload_hash,
                {"Tier_A_1k": 11, "Tier_B_10k": 21, "Tier_C_50k": 42, "Tier_D_100k": 84},
            )
            result = subprocess.run([
                "node", self.script,
                "--baseline-artifact", baseline,
                "--candidate-artifact", candidate,
                "--baseline-commit", "before-commit",
                "--candidate-commit", "candidate-commit",
                "--workload-hash", workload_hash,
                "--output", output,
            ], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertNotEqual(result.returncode, 0)
            with open(output, "r", encoding="utf-8") as stream:
                evidence = json.load(stream)
            self.assertEqual(evidence["status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
