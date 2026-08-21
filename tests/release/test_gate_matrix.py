import json
import hashlib
import gzip
import os
import sys
import tempfile
import unittest

from scripts.diagnostics.gate_matrix import (
    _configured_parser_preflight, _external, _revision, build,
)
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

    def test_verified_backup_requires_independent_provenance_manifest(self):
        old = os.environ.get("COVERAGE_GATE_TEST_EVIDENCE")
        with tempfile.TemporaryDirectory() as directory:
            dump = os.path.join(directory, "full.sql.gz")
            with gzip.open(dump, "wb") as stream:
                stream.write(b"CREATE TABLE coverage_example (id INT);\n")
            with open(dump, "rb") as stream:
                dump_sha = hashlib.sha256(stream.read()).hexdigest()
            provenance_path = os.path.join(directory, "backup-manifest.json")
            provenance = {
                "status": "BACKUP_VERIFIED",
                "evidence_class": "production_backup",
                "synthetic": False,
                "backup_root_external": True,
                "database": "coverage",
                "full_sql_gz_size": os.path.getsize(dump),
                "full_sql_gz_sha256": dump_sha,
                "snapshot": {"tables": {"coverage_analysis": {"count": 1}}},
                "verification": {
                    "table_inventory": ["coverage_example"],
                    "restore_smoke": "PASSED",
                    "restore_target_empty_before_restore": True,
                    "restore_database_runtime_identity": {"version": "11.8"},
                },
                "provenance": {
                    "source_environment": "production",
                    "operator": "release-operator",
                    "attested_at": "2026-08-21T00:00:00Z",
                },
            }
            with open(provenance_path, "w", encoding="utf-8") as stream:
                json.dump(provenance, stream)
            with open(provenance_path, "rb") as stream:
                provenance_sha = hashlib.sha256(stream.read()).hexdigest()

            evidence_path = os.path.join(directory, "gate-a.json")
            base = {
                "status": "PASSED",
                "gate": "gate-a",
                "candidate_revision": self.revision,
                "host_identity": {"hostname": "test"},
                "evidence_class": "verified_production_backup_restore_rehearsal",
                "command_or_action": "verified backup rehearsal",
                "started_at": "2026-08-21T00:00:00Z",
                "finished_at": "2026-08-21T00:00:01Z",
                "exit_code": 0,
                "artifact_path": dump,
                "artifact_sha256": dump_sha,
                "release_identity": {"commit_sha": self.revision},
                "synthetic": False,
            }
            os.environ["COVERAGE_GATE_TEST_EVIDENCE"] = evidence_path
            with open(evidence_path, "w", encoding="utf-8") as stream:
                json.dump(base, stream)
            rejected = _external(
                "verified_backup_restore", "verified production backup",
                "COVERAGE_GATE_TEST_EVIDENCE", self.revision,
                self.repo_root, "gate-a",
            )
            self.assertEqual(rejected["status"], "INCOMPLETE")
            self.assertTrue(any("backup_provenance" in item for item in rejected["violations"]))

            base["backup_provenance"] = {
                "manifest_path": provenance_path,
                "manifest_sha256": provenance_sha,
            }
            with open(evidence_path, "w", encoding="utf-8") as stream:
                json.dump(base, stream)
            accepted = _external(
                "verified_backup_restore", "verified production backup",
                "COVERAGE_GATE_TEST_EVIDENCE", self.revision,
                self.repo_root, "gate-a",
            )
            self.assertEqual(accepted["status"], "PASSED")
            self.assertEqual(accepted["violations"], [])
        if old is None:
            os.environ.pop("COVERAGE_GATE_TEST_EVIDENCE", None)
        else:
            os.environ["COVERAGE_GATE_TEST_EVIDENCE"] = old

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

    def test_parser_preflight_uses_the_runtime_configured_adapter(self):
        helper_source = r'''
import json
import sys

if "--version" in sys.argv:
    print("fixture-parser 1")
    raise SystemExit(0)
if "--analyze-json" not in sys.argv:
    raise SystemExit(2)
request = json.load(sys.stdin)
print(json.dumps({
    "protocol": "coverage-cpp-parser-v1",
    "analysis": {
        "supported": True,
        "path": request["path"],
        "functions": [{
            "identity": {"path": request["path"], "scope": [],
                         "name": "preflight_function", "parameters": [],
                         "qualifiers": [], "trailing_return": []},
            "start_line": 1, "end_line": 1,
        }],
        "controls": {}, "preprocessor": {}, "macros": {},
        "constants": {}, "calls": {}, "uncertain": False,
    },
}))
'''
        with tempfile.TemporaryDirectory(prefix="gate-parser-config-") as directory:
            helper = os.path.join(directory, "helper.py")
            with open(helper, "w") as stream:
                stream.write(helper_source)
            config = os.path.join(directory, "candidate.json")
            with open(config, "w") as stream:
                json.dump({
                    "runtime_mode": "vnext",
                    "schema_version": 1,
                    "server": {"host": "127.0.0.1", "port": 19528},
                    "auth": {"mode": "disabled"},
                    "inheritance_parser": {
                        "adapter": "json-cli-v1",
                        "command": [sys.executable, helper],
                        "require_external": True,
                    },
                }, stream)
            result = _configured_parser_preflight(self.repo_root, config)
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["backend"], "json-cli-v1")
        self.assertEqual(result["configuration_source"], "inheritance_parser")
        self.assertTrue(result["runtime_config_path"].endswith("candidate.json"))
if __name__ == "__main__":
    unittest.main()
