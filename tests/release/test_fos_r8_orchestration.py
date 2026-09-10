import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

from app.release_identity import generate_release_identity, save_release_manifest
from scripts.release.build_oneclick_release import build as build_oneclick
from scripts.release.current_adoption import (
    bootstrap_flat_current, plan_flat_current_adoption,
)
from scripts.release.prepare_legacy_flat_adoption import (
    flat_source_binding, verify_flat_source_binding,
)
from scripts.upgrade.performance_evidence import join_release_performance


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
BASELINE_SHA = "e9fcc837a1ac9847f3966fc8ddb2aed92ca473fc"


def _write_json(path, payload):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")


def _performance(path, revision, workload_hash, values, environment=None,
                 synthetic=False):
    tiers = {
        name: {"status": "PASSED", "measured_ms": value}
        for name, value in values.items()
    }
    _write_json(path, {
        "status": "PASSED",
        "evidence_class": "release_performance_revision",
        "comparison_type": "single_revision",
        "synthetic": synthetic,
        "revision": revision,
        "workload_id": "r8-test-http-workload-v1",
        "workload_hash": workload_hash,
        "environment_identity": environment or {
            "browser_engine": "chromium",
            "browser_version": "test-browser",
            "platform": "linux",
            "arch": "x86_64",
            "runner_class": "exact-revision-real-http-chromium",
        },
        "exit_code": 0,
        "tiers": tiers,
        "coverage_virtual_scroll_100k": {
            "status": "PASSED",
            "elapsed_ms": values["Tier_D_100k"],
            "logical_line_count": 100000,
        },
    })


class FlatBindingRegressionTest(unittest.TestCase):
    def _flat_fixture(self, root):
        flat = os.path.join(root, "flat")
        identity_source = os.path.join(root, "identity-source")
        os.makedirs(flat)
        os.makedirs(identity_source)
        asset = b"coverage-progress-r8\n"
        with open(os.path.join(flat, "coverage_progress.js"), "wb") as stream:
            stream.write(asset)
        with open(os.path.join(identity_source, "coverage_progress.js"), "wb") as stream:
            stream.write(asset)
        with open(os.path.join(flat, "index.html"), "w", encoding="utf-8") as stream:
            stream.write("<html><head><title>legacy</title></head><body>report</body></html>")
        identity = generate_release_identity(
            identity_source, commit_sha=BASELINE_SHA,
            asset_files=[os.path.join(identity_source, "coverage_progress.js")],
        )
        identity_path = os.path.join(root, "release_identity.json")
        save_release_manifest(identity_path, identity)
        return flat, identity_path

    def test_binding_is_deterministic_and_catches_source_mutation(self):
        with tempfile.TemporaryDirectory(prefix="r8-flat-binding-") as root:
            flat, identity_path = self._flat_fixture(root)
            binding = flat_source_binding(flat, identity_path, BASELINE_SHA)
            self.assertEqual(binding["source_kind"], "legacy_flat_root")
            self.assertEqual(binding["previous_release_commit_sha"], BASELINE_SHA)
            self.assertEqual(binding["legacy_source_file_count"], 2)
            self.assertEqual(
                verify_flat_source_binding(
                    flat, identity_path, BASELINE_SHA, binding
                ),
                binding,
            )
            with open(os.path.join(flat, "coverage_progress.js"), "ab") as stream:
                stream.write(b"changed")
            with self.assertRaisesRegex(ValueError, "source binding changed"):
                verify_flat_source_binding(flat, identity_path, BASELINE_SHA, binding)

    def test_bootstrap_refuses_changed_binding_before_current_is_created(self):
        with tempfile.TemporaryDirectory(prefix="r8-flat-bootstrap-") as root:
            flat, identity_path = self._flat_fixture(root)
            publish = os.path.join(root, "publish")
            binding = plan_flat_current_adoption(
                publish, flat, identity_path, BASELINE_SHA
            )["flat_source_binding"]
            with open(os.path.join(flat, "coverage_progress.js"), "ab") as stream:
                stream.write(b"changed")
            with self.assertRaisesRegex(ValueError, "source binding changed"):
                bootstrap_flat_current(
                    publish, flat, identity_path, BASELINE_SHA, "baseline",
                    switch=True, expected_source_binding=binding,
                )
            self.assertFalse(os.path.lexists(os.path.join(publish, "CURRENT")))


class PerformanceJoinRegressionTest(unittest.TestCase):
    def test_join_requires_two_exact_non_synthetic_revisions(self):
        with tempfile.TemporaryDirectory(prefix="r8-performance-join-") as root:
            workload = "sha256:r8-test-workload-v1"
            baseline = os.path.join(root, "baseline.json")
            candidate = os.path.join(root, "candidate.json")
            output = os.path.join(root, "release-ab.json")
            values = {
                "Tier_A_1k": 10, "Tier_B_10k": 20,
                "Tier_C_50k": 40, "Tier_D_100k": 80,
            }
            candidate_values = {
                "Tier_A_1k": 11, "Tier_B_10k": 21,
                "Tier_C_50k": 42, "Tier_D_100k": 84,
            }
            _performance(baseline, BASELINE_SHA, workload, values)
            candidate_sha = "1" * 40
            _performance(candidate, candidate_sha, workload, candidate_values)
            result = join_release_performance(
                baseline, candidate, BASELINE_SHA, candidate_sha, workload,
                output, release_validation_session_id="r8-session",
                candidate_artifact_sha256="a" * 64,
                served_root_sha256="b" * 64,
                max_regression_percent=20,
            )
            self.assertEqual(result["status"], "PASSED")
            self.assertEqual(result["evidence_class"], "release_performance_ab")
            self.assertEqual(result["comparison_type"], "release_revision_ab")
            self.assertFalse(result["synthetic"])
            self.assertEqual(result["release_validation_session_id"], "r8-session")
            self.assertTrue(os.path.isfile(output))

            _performance(candidate, candidate_sha, workload, candidate_values,
                         synthetic=True)
            with self.assertRaisesRegex(ValueError, "synthetic"):
                join_release_performance(
                    baseline, candidate, BASELINE_SHA, candidate_sha, workload,
                    output,
                )

    def test_join_rejects_environment_mismatch_and_partial_publication_binding(self):
        with tempfile.TemporaryDirectory(prefix="r8-performance-binding-") as root:
            workload = "sha256:r8-test-workload-v1"
            baseline = os.path.join(root, "baseline.json")
            candidate = os.path.join(root, "candidate.json")
            output = os.path.join(root, "release-ab.json")
            values = {
                "Tier_A_1k": 10, "Tier_B_10k": 20,
                "Tier_C_50k": 40, "Tier_D_100k": 80,
            }
            _performance(baseline, BASELINE_SHA, workload, values)
            _performance(
                candidate, "2" * 40, workload, values,
                environment={"browser_engine": "other", "platform": "linux"},
            )
            with self.assertRaisesRegex(ValueError, "environment"):
                join_release_performance(
                    baseline, candidate, BASELINE_SHA, "2" * 40, workload,
                    output,
                )
            _performance(candidate, "2" * 40, workload, values)
            with self.assertRaisesRegex(ValueError, "publication binding"):
                join_release_performance(
                    baseline, candidate, BASELINE_SHA, "2" * 40, workload,
                    output, release_validation_session_id="session-only",
                )


class OneClickAndBoundaryRegressionTest(unittest.TestCase):
    def _git(self, root, *args):
        return subprocess.check_output(["git"] + list(args), cwd=root).decode().strip()

    def _source_repo_and_bundle(self, root):
        source = os.path.join(root, "source")
        os.makedirs(source)
        with open(os.path.join(source, "README.md"), "w", encoding="utf-8") as stream:
            stream.write("baseline\n")
        subprocess.check_call(["git", "init", "-q"], cwd=source)
        subprocess.check_call(["git", "config", "user.email", "r8@test.invalid"], cwd=source)
        subprocess.check_call(["git", "config", "user.name", "R8 Test"], cwd=source)
        subprocess.check_call(["git", "add", "README.md"], cwd=source)
        subprocess.check_call(["git", "commit", "-q", "-m", "baseline"], cwd=source)
        baseline = self._git(source, "rev-parse", "HEAD")
        with open(os.path.join(source, "README.md"), "a", encoding="utf-8") as stream:
            stream.write("candidate\n")
        subprocess.check_call(["git", "add", "README.md"], cwd=source)
        subprocess.check_call(["git", "commit", "-q", "-m", "candidate"], cwd=source)
        final = self._git(source, "rev-parse", "HEAD")
        tree = self._git(source, "rev-parse", "HEAD^{tree}")
        bundle = os.path.join(root, "source.bundle")
        subprocess.check_call(
            ["git", "bundle", "create", bundle, "HEAD"], cwd=source
        )
        return source, bundle, baseline, final, tree

    def test_oneclick_generator_embeds_and_reextracts_exact_inputs(self):
        with tempfile.TemporaryDirectory(prefix="r8-oneclick-") as root:
            source, bundle, baseline, final, tree = self._source_repo_and_bundle(root)
            workload = "sha256:r8-oneclick-workload-v1"
            baseline_perf = os.path.join(root, "baseline-performance.json")
            candidate_perf = os.path.join(root, "candidate-performance.json")
            values = {
                "Tier_A_1k": 10, "Tier_B_10k": 20,
                "Tier_C_50k": 40, "Tier_D_100k": 80,
            }
            _performance(baseline_perf, baseline, workload, values)
            _performance(candidate_perf, final, workload, values)
            metadata_path = os.path.join(root, "metadata.json")
            _write_json(metadata_path, {
                "repository": "Chary-yu/fos_coverage_tool",
                "production_host": "vfoswind",
                "final_r8_sha": final,
                "final_r8_tree": tree,
                "production_baseline_sha": baseline,
                "workload_hash": workload,
                "performance_max_regression_percent": 20,
            })
            output = os.path.join(root, "release.txt")
            args = type("Args", (), {
                "repo_root": source,
                "final_sha": final,
                "final_tree": tree,
                "baseline_sha": baseline,
                "source_bundle": bundle,
                "baseline_performance": baseline_perf,
                "candidate_performance": candidate_perf,
                "workload_hash": workload,
                "metadata": metadata_path,
                "output": output,
            })()
            result = build_oneclick(args)
            self.assertEqual(result["status"], "PASSED")
            subprocess.check_call(["bash", "-n", output])
            state = os.path.join(root, "state")
            non_git_cwd = os.path.join(root, "non-git-cwd")
            os.makedirs(non_git_cwd)
            env = dict(os.environ, FOS_R8_STATE_ROOT=state)
            verify = subprocess.check_output(
                ["bash", output, "--verify-only"], env=env,
                cwd=non_git_cwd,
                stderr=subprocess.STDOUT,
            ).decode()
            self.assertIn("EXACT_SOURCE_BUNDLE=PASS", verify)
            self.assertIn("PRODUCTION_MUTATION=NONE", verify)
            repeat = subprocess.check_output(
                ["bash", output, "--resume", "--verify-only"], env=env,
                stderr=subprocess.STDOUT,
            ).decode()
            self.assertIn("EXACT_PERFORMANCE_INPUTS=PASS", repeat)
            self.assertTrue(os.path.isfile(os.path.join(state, "input", "source.bundle")))

            _write_json(os.path.join(state, "state.json"), {
                "phase": "C",
                "status": "CANDIDATE_VALIDATING",
            })
            _write_json(os.path.join(state, "upgrade-state.json"), {
                "state": "PRE_CUTOVER_READY",
            })
            status_before_apply = subprocess.check_output(
                ["bash", output, "--status"], env=env, cwd=non_git_cwd,
                stderr=subprocess.STDOUT,
            ).decode()
            self.assertNotIn("CUTOVER_IN_PROGRESS", status_before_apply)
            self.assertNotIn('"phase": "D"', status_before_apply)

            _write_json(os.path.join(state, "upgrade-state.json"), {
                "state": "CUTTING_OVER",
            })
            status_after_apply = subprocess.check_output(
                ["bash", output, "--status"], env=env, cwd=non_git_cwd,
                stderr=subprocess.STDOUT,
            ).decode()
            self.assertIn("CUTTING_OVER", status_after_apply)

    def test_source_gate_strings_keep_apply_after_pre_cutover(self):
        path = os.path.join(ROOT, "scripts", "upgrade", "run_upgrade.py")
        with open(path, "r", encoding="utf-8") as stream:
            source = stream.read()
        self.assertLess(
            source.rfind("ready, unmet = self._validate_pre_cutover_ready"),
            source.rfind("if not self._require_production_mutation_confirmation"),
        )
        self.assertLess(
            source.rfind("if not self._require_production_mutation_confirmation"),
            source.rfind("self._phase_d_entered = True"),
        )
        self.assertIn("PRODUCTION_MUTATION", source)

    def test_conductor_projects_canonical_production_manifest(self):
        path = os.path.join(ROOT, "scripts", "release", "fos_r8_conductor.py")
        with open(path, "r", encoding="utf-8") as stream:
            source = stream.read()
        self.assertIn(
            "from scripts.upgrade.evidence_manifest import MANIFEST_FILENAME",
            source,
        )
        self.assertIn(
            "manifest_path = os.path.join(evidence_root, MANIFEST_FILENAME)",
            source,
        )
        self.assertNotIn(
            'manifest_path = os.path.join(evidence_root, "evidence_manifest.json")',
            source,
        )


if __name__ == "__main__":
    unittest.main()
