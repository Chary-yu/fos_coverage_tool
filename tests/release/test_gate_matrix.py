import json
import hashlib
import os
import tempfile
import unittest

from scripts.diagnostics.gate_matrix import _external, _revision, build
from scripts.upgrade.evidence_manifest import EvidenceManifestV2


class GateMatrixEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
        self.revision = _revision(self.repo_root)

    def test_external_path_must_exist_and_be_authentic_json(self):
        old = os.environ.get("COVERAGE_GATE_TEST_EVIDENCE")
        try:
            os.environ["COVERAGE_GATE_TEST_EVIDENCE"] = os.path.join(
                tempfile.gettempdir(), "coverage-gate-evidence-does-not-exist.json"
            )
            result = _external(
                "test", "test evidence", "COVERAGE_GATE_TEST_EVIDENCE",
                self.revision, self.repo_root, "gate-a",
            )
            self.assertEqual(result["status"], "INCOMPLETE")
            self.assertTrue(result["violations"])
        finally:
            if old is None:
                os.environ.pop("COVERAGE_GATE_TEST_EVIDENCE", None)
            else:
                os.environ["COVERAGE_GATE_TEST_EVIDENCE"] = old

    def test_external_pass_requires_complete_provenance_and_artifact(self):
        fd, path = tempfile.mkstemp(prefix="coverage-gate-evidence-", suffix=".json")
        os.close(fd)
        artifact = path + ".payload"
        old = os.environ.get("COVERAGE_GATE_TEST_EVIDENCE")
        try:
            with open(artifact, "w", encoding="utf-8") as stream:
                stream.write("verified external result\n")
            with open(artifact, "rb") as stream:
                artifact_sha = hashlib.sha256(stream.read()).hexdigest()
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({
                    "status": "PASSED",
                    "gate": "gate-a",
                    "candidate_revision": self.revision,
                    "host_identity": {"hostname": "test"},
                    "evidence_class": "external_test",
                    "command_or_action": "test evidence",
                    "started_at": "2026-08-21T00:00:00Z",
                    "finished_at": "2026-08-21T00:00:01Z",
                    "exit_code": 0,
                    "artifact_path": artifact,
                    "artifact_sha256": artifact_sha,
                    "release_identity": {"commit_sha": self.revision},
                    "synthetic": False,
                }, stream)
            os.environ["COVERAGE_GATE_TEST_EVIDENCE"] = path
            result = _external(
                "test", "test evidence", "COVERAGE_GATE_TEST_EVIDENCE",
                self.revision, self.repo_root, "gate-a",
            )
            self.assertEqual(result["status"], "PASSED")
            self.assertEqual(result["violations"], [])
        finally:
            if old is None:
                os.environ.pop("COVERAGE_GATE_TEST_EVIDENCE", None)
            else:
                os.environ["COVERAGE_GATE_TEST_EVIDENCE"] = old
            try:
                os.remove(path)
            except OSError:
                pass
            try:
                os.remove(artifact)
            except OSError:
                pass

    def test_external_pass_without_provenance_is_incomplete(self):
        fd, path = tempfile.mkstemp(prefix="coverage-gate-incomplete-", suffix=".json")
        os.close(fd)
        old = os.environ.get("COVERAGE_GATE_TEST_EVIDENCE")
        try:
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({
                    "status": "PASSED",
                    "gate": "gate-a",
                    "candidate_revision": self.revision,
                    "host_identity": {"hostname": "test"},
                    "command_or_action": "test evidence",
                    "synthetic": False,
                }, stream)
            os.environ["COVERAGE_GATE_TEST_EVIDENCE"] = path
            result = _external(
                "test", "test evidence", "COVERAGE_GATE_TEST_EVIDENCE",
                self.revision, self.repo_root, "gate-a",
            )
            self.assertEqual(result["status"], "INCOMPLETE")
            self.assertTrue(any("timestamps" in item for item in result["violations"]))
            self.assertTrue(any("artifact" in item for item in result["violations"]))
        finally:
            if old is None:
                os.environ.pop("COVERAGE_GATE_TEST_EVIDENCE", None)
            else:
                os.environ["COVERAGE_GATE_TEST_EVIDENCE"] = old
            try:
                os.remove(path)
            except OSError:
                pass

    def test_external_evidence_cannot_be_replayed_into_another_gate(self):
        fd, path = tempfile.mkstemp(prefix="coverage-gate-wrong-gate-", suffix=".json")
        os.close(fd)
        artifact = path + ".payload"
        old = os.environ.get("COVERAGE_GATE_TEST_EVIDENCE")
        try:
            with open(artifact, "w", encoding="utf-8") as stream:
                stream.write("gate-a artifact\n")
            with open(artifact, "rb") as stream:
                artifact_sha = hashlib.sha256(stream.read()).hexdigest()
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({
                    "status": "PASSED",
                    "gate": "gate-a",
                    "candidate_revision": self.revision,
                    "release_identity": {"commit_sha": self.revision},
                    "host_identity": {"hostname": "test"},
                    "evidence_class": "external_test",
                    "command_or_action": "test evidence",
                    "started_at": "2026-08-21T00:00:00Z",
                    "finished_at": "2026-08-21T00:00:01Z",
                    "exit_code": 0,
                    "artifact_path": artifact,
                    "artifact_sha256": artifact_sha,
                    "synthetic": False,
                }, stream)
            os.environ["COVERAGE_GATE_TEST_EVIDENCE"] = path
            result = _external(
                "test", "test evidence", "COVERAGE_GATE_TEST_EVIDENCE",
                self.revision, self.repo_root, "gate-b",
            )
            self.assertEqual(result["status"], "INCOMPLETE")
            self.assertTrue(any("gate" in item for item in result["violations"]))
        finally:
            if old is None:
                os.environ.pop("COVERAGE_GATE_TEST_EVIDENCE", None)
            else:
                os.environ["COVERAGE_GATE_TEST_EVIDENCE"] = old
            for target in (path, artifact):
                try:
                    os.remove(target)
                except OSError:
                    pass

    def test_cross_layer_performance_requires_explicit_release_eligibility(self):
        fd, path = tempfile.mkstemp(prefix="coverage-gate-perf-", suffix=".json")
        os.close(fd)
        artifact = path + ".payload"
        old = os.environ.get("COVERAGE_GATE_TEST_EVIDENCE")
        base = {
            "status": "PASSED",
            "gate": "gate-e",
            "candidate_revision": self.revision,
            "host_identity": {"hostname": "test"},
            "evidence_class": "real_http_chromium_performance",
            "command_or_action": "test performance evidence",
            "started_at": "2026-08-21T00:00:00Z",
            "finished_at": "2026-08-21T00:00:01Z",
            "exit_code": 0,
            "release_identity": {"commit_sha": self.revision},
            "synthetic": False,
        }
        try:
            with open(artifact, "w", encoding="utf-8") as stream:
                stream.write("cross-layer performance artifact\n")
            with open(artifact, "rb") as stream:
                base["artifact_sha256"] = hashlib.sha256(stream.read()).hexdigest()
            base["artifact_path"] = artifact
            with open(path, "w", encoding="utf-8") as stream:
                json.dump(base, stream)
            os.environ["COVERAGE_GATE_TEST_EVIDENCE"] = path
            incomplete = _external(
                "cross_layer_performance", "cross-layer performance",
                "COVERAGE_GATE_TEST_EVIDENCE", self.revision,
                self.repo_root, "gate-e",
            )
            self.assertEqual(incomplete["status"], "INCOMPLETE")
            self.assertTrue(any(
                "release_eligible=true" in item
                for item in incomplete["violations"]
            ))

            base["release_eligible"] = True
            with open(path, "w", encoding="utf-8") as stream:
                json.dump(base, stream)
            passed = _external(
                "cross_layer_performance", "cross-layer performance",
                "COVERAGE_GATE_TEST_EVIDENCE", self.revision,
                self.repo_root, "gate-e",
            )
            self.assertEqual(passed["status"], "PASSED")
        finally:
            if old is None:
                os.environ.pop("COVERAGE_GATE_TEST_EVIDENCE", None)
            else:
                os.environ["COVERAGE_GATE_TEST_EVIDENCE"] = old
            for target in (path, artifact):
                try:
                    os.remove(target)
                except OSError:
                    pass

    def test_cross_layer_manifest_requires_explicit_release_eligibility(self):
        old = os.environ.get("COVERAGE_GATE_TEST_EVIDENCE")
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = os.path.join(directory, "gate-e-manifest.json")
            artifact = os.path.join(directory, "performance.json")
            with open(artifact, "w", encoding="utf-8") as stream:
                stream.write("cross-layer manifest artifact\n")
            manifest = EvidenceManifestV2(
                self.repo_root, "gate-e", candidate_revision=self.revision,
                release_identity={"commit_sha": self.revision},
                manifest_path=manifest_path,
            )
            manifest.record(
                "cross-layer", "real_http_chromium_performance", "PASSED",
                command_or_action="test performance manifest",
                exit_code=0, artifact_path=artifact, synthetic=False,
                release_identity={"commit_sha": self.revision},
                release_eligible=False,
            )
            os.environ["COVERAGE_GATE_TEST_EVIDENCE"] = manifest_path
            incomplete = _external(
                "cross_layer_performance", "cross-layer performance",
                "COVERAGE_GATE_TEST_EVIDENCE", self.revision,
                self.repo_root, "gate-e",
            )
            self.assertEqual(incomplete["status"], "INCOMPLETE")
            self.assertTrue(any(
                "release_eligible=true" in item
                for item in incomplete["violations"]
            ))

            manifest.record(
                "cross-layer", "real_http_chromium_performance", "PASSED",
                command_or_action="test performance manifest",
                exit_code=0, artifact_path=artifact, synthetic=False,
                release_identity={"commit_sha": self.revision},
                release_eligible=True,
            )
            passed = _external(
                "cross_layer_performance", "cross-layer performance",
                "COVERAGE_GATE_TEST_EVIDENCE", self.revision,
                self.repo_root, "gate-e",
            )
            self.assertEqual(passed["status"], "PASSED")
        if old is None:
            os.environ.pop("COVERAGE_GATE_TEST_EVIDENCE", None)
        else:
            os.environ["COVERAGE_GATE_TEST_EVIDENCE"] = old

    def test_gate_f_includes_exact_source_and_security_reviews(self):
        matrix = build(self.repo_root)
        checks = {
            item["name"]: item
            for item in matrix["gates"]["F"]["local_checks"]
        }
        self.assertIn("final_source_review", checks)
        self.assertIn("final_security_review", checks)
        self.assertEqual(
            checks["final_security_review"]["evidence_class"],
            "exact_sha_security_review",
        )
if __name__ == "__main__":
    unittest.main()
