import json
import os
import tempfile
import unittest

from scripts.diagnostics.release_performance_ab import build_release_performance_ab
from scripts.upgrade.run_airgapped_upgrade import _external_origin, _same_origin


BASELINE = "b" * 40
CANDIDATE = "c" * 40
WORKLOAD_HASH = "workload-hash-v1"


def _revision_payload(revision, measured=100.0, environment=None):
    environment = environment or {
        "browser_name": "chromium",
        "browser_version": "128.0.0.0",
        "node_version": "v20.0.0",
        "platform": "linux",
        "arch": "x64",
        "viewport": "1280x800",
        "workload_driver": "playwright",
    }
    tiers = {}
    for name in ("Tier_A_1k", "Tier_B_10k", "Tier_C_50k", "Tier_D_100k"):
        tiers[name] = {
            "status": "PASSED",
            "measured_ms": measured,
        }
    return {
        "status": "PASSED",
        "evidence_class": "release_performance_revision",
        "comparison_type": "single_revision",
        "revision": revision,
        "workload_id": "coverage-release-browser-v1",
        "workload_hash": WORKLOAD_HASH,
        "environment_identity": environment,
        "tiers": tiers,
        "coverage_virtual_scroll_100k": {
            "status": "PASSED",
            "elapsed_ms": measured,
        },
        "exit_code": 0,
    }


class AirGappedPerformanceTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(prefix="r8-airgap-perf-")
        self.addCleanup(self.root.cleanup)

    def _write(self, name, payload):
        path = os.path.join(self.root.name, name)
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
        return path

    def test_python_combiner_accepts_independent_exact_revision_sources(self):
        baseline = self._write("baseline.json", _revision_payload(BASELINE, 100.0))
        candidate = self._write("candidate.json", _revision_payload(CANDIDATE, 110.0))
        output = os.path.join(self.root.name, "combined.json")
        result = build_release_performance_ab(
            baseline, candidate, BASELINE, CANDIDATE, WORKLOAD_HASH, output,
            release_validation_session_id="candidate-session",
            candidate_artifact_sha256="a" * 64,
            served_root_sha256="d" * 64,
            max_regression_percent=20,
        )
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["candidate_commit"], CANDIDATE)
        self.assertEqual(result["baseline_commit"], BASELINE)
        self.assertEqual(result["source_artifacts"]["candidate"]["revision"], CANDIDATE)
        self.assertEqual(result["release_validation_session_id"], "candidate-session")
        self.assertTrue(os.path.isfile(output))

    def test_python_combiner_rejects_environment_drift(self):
        baseline = self._write("baseline.json", _revision_payload(BASELINE))
        changed = _revision_payload(CANDIDATE)
        changed["environment_identity"] = dict(changed["environment_identity"])
        changed["environment_identity"]["browser_version"] = "129.0.0.0"
        candidate = self._write("candidate.json", changed)
        with self.assertRaisesRegex(RuntimeError, "environment_identity"):
            build_release_performance_ab(
                baseline, candidate, BASELINE, CANDIDATE, WORKLOAD_HASH,
                os.path.join(self.root.name, "combined.json"),
            )

    def test_python_combiner_rejects_same_run_synthetic_identity(self):
        baseline_payload = _revision_payload(BASELINE)
        baseline_payload["comparison_type"] = "synthetic_same_run"
        baseline = self._write("baseline.json", baseline_payload)
        candidate = self._write("candidate.json", _revision_payload(CANDIDATE))
        with self.assertRaisesRegex(RuntimeError, "release_performance_revision"):
            build_release_performance_ab(
                baseline, candidate, BASELINE, CANDIDATE, WORKLOAD_HASH,
                os.path.join(self.root.name, "combined.json"),
            )

    def test_python_combiner_fails_regression_budget_without_promoting_pass(self):
        baseline = self._write("baseline.json", _revision_payload(BASELINE, 100.0))
        candidate = self._write("candidate.json", _revision_payload(CANDIDATE, 150.0))
        result = build_release_performance_ab(
            baseline, candidate, BASELINE, CANDIDATE, WORKLOAD_HASH,
            os.path.join(self.root.name, "combined.json"),
            max_regression_percent=20,
        )
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["Tier_D_100k"]["status"], "FAILED")

    def test_gateway_origin_accepts_report_on_same_external_origin(self):
        origin = _external_origin("https://candidate-gateway.vfoswind.internal")
        self.assertEqual(origin, "https://candidate-gateway.vfoswind.internal")
        self.assertTrue(_same_origin(
            "https://candidate-gateway.vfoswind.internal/coverage/actual-report.html",
            origin,
        ))
        self.assertFalse(_same_origin(
            "https://other.internal/coverage/actual-report.html",
            origin,
        ))

    def test_gateway_origin_rejects_loopback_and_paths(self):
        with self.assertRaises(RuntimeError):
            _external_origin("http://127.0.0.1:19529")
        with self.assertRaises(RuntimeError):
            _external_origin("https://candidate-gateway.vfoswind.internal/coverage/report.html")


if __name__ == "__main__":
    unittest.main()
