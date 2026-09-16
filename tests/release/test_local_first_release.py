import base64
import json
import os
import tempfile
import unittest
from unittest import mock


class LocalFirstReleaseTest(unittest.TestCase):
    def test_github_actions_workflows_are_retired(self):
        from scripts.diagnostics.production_release_authority import evaluate

        for relative in (
                ".github/workflows/ci.yml",
                ".github/workflows/trusted-candidate-builder.yml",
                ".github/workflows/trusted-production-candidate-builder.yml",
                ".github/workflows/r8-production-compat-regression.yml",
                ".github/workflows/r8-release-performance.yml"):
            with self.subTest(path=relative):
                self.assertFalse(os.path.exists(relative))

        contract = evaluate(os.getcwd())
        self.assertEqual(contract["GITHUB_WORKFLOW_RESIDUE"], "NO")
        self.assertEqual(contract["LOCAL_FIRST_AUTHORITY_READY"], "YES")

    def test_production_authority_is_offline_and_has_no_github_readiness_dependency(self):
        from scripts.diagnostics.production_release_authority import (
            evaluate, render_contract,
        )

        contract = evaluate(os.getcwd())
        self.assertEqual(contract["PRODUCTION_RELEASE_AUTHORITY"], "OFFLINE_OPERATOR_ONLY")
        self.assertEqual(contract["GITHUB_ACTIONS_REQUIRED"], "NO")
        self.assertEqual(contract["GITHUB_API_REQUIRED_FOR_READINESS"], "NO")
        self.assertEqual(contract["PROTECTED_BUILDER_NEW_PUBLICATION_ALLOWED"], "NO")
        self.assertEqual(contract["DUAL_PRODUCTION_AUTHORITY"], "NO")
        self.assertEqual(contract["STALE_GITHUB_WORKFLOW_AFFECTS_RELEASE"], "NO")
        rendered = render_contract(contract)
        self.assertIn("PRODUCTION_RELEASE_AUTHORITY=OFFLINE_OPERATOR_ONLY", rendered)
        self.assertIn("GITHUB_SOURCE_REPOSITORY_ONLY=YES", rendered)
        self.assertIn("STALE_GITHUB_WORKFLOW_AFFECTS_RELEASE=NO", rendered)

    def test_release_readiness_local_first_reports_current_authority(self):
        from scripts.diagnostics.release_readiness import build

        result = build(
            os.getcwd(), matrix={}, task_status={}, risk_register={},
            dod_status={}, local_first=True,
        )
        self.assertEqual(result["status"], "NOT_READY")
        self.assertFalse(any(
            item.get("type") == "production_release_authority"
            for item in result["blockers"]
        ))
        self.assertEqual(
            result["production_release_authority"]["GITHUB_WORKFLOW_RESIDUE"],
            "NO",
        )

    def test_evidence_manifest_detects_tampered_json(self):
        from scripts.release.local_release_gate import (
            build_evidence_manifest, verify_evidence_manifest,
        )

        with tempfile.TemporaryDirectory(prefix="evidence-manifest-") as root:
            path = os.path.join(root, "source_identity.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({"source": "exact"}, stream)
            manifest_path = build_evidence_manifest(
                root, source_sha="a" * 40, session_id="session-1"
            )
            self.assertTrue(verify_evidence_manifest(root, manifest_path))
            with open(path, "a", encoding="utf-8") as stream:
                stream.write("\n tampered\n")
            self.assertFalse(verify_evidence_manifest(root, manifest_path))

    def test_new_release_path_does_not_contain_live_github_api_calls(self):
        paths = (
            "scripts/release/local_release_gate.py",
            "scripts/release/generate_flat_production_candidate_operator.py",
            "scripts/release/flat_candidate_operator_runtime.py",
            "scripts/diagnostics/production_release_authority.py",
        )
        for relative in paths:
            with self.subTest(path=relative):
                with open(relative, encoding="utf-8") as stream:
                    text = stream.read().lower()
                self.assertNotIn("api.github.com", text)
                self.assertNotIn("gh run", text)

    def test_orchestration_validator_checks_generic_offline_operator_contract(self):
        from scripts.validate_release_orchestration import validate

        result = validate(os.getcwd())
        names = {item["name"] for item in result["checks"]}
        self.assertIn(
            "required_file:scripts/release/release_input_manifest.py", names
        )
        self.assertIn(
            "required_file:scripts/release/generate_flat_production_candidate_operator.py",
            names,
        )
        for required in (
                "scripts/release/local_release_gate.py",
                "scripts/release/local_compatibility_gate.py",
                "scripts/release/compatibility_surface.py",
                "scripts/diagnostics/production_release_authority.py"):
            self.assertIn("required_file:" + required, names)
        self.assertIn("generic_offline_source_transfer", names)
        self.assertEqual(result["status"], "PASSED")

    def test_release_gate_requires_exact_clean_source_identity(self):
        from scripts.release.local_release_gate import _source_identity_ready

        identity = {
            "head_sha": "a" * 40,
            "tree_sha": "b" * 40,
            "worktree_clean": True,
            "clean_checkout": True,
        }
        self.assertTrue(_source_identity_ready(identity, "a" * 40))
        identity["worktree_clean"] = False
        self.assertFalse(_source_identity_ready(identity, "a" * 40))
        identity["worktree_clean"] = True
        self.assertFalse(_source_identity_ready(identity, "c" * 40))

    def test_deferred_compatibility_is_staging_only(self):
        from scripts.release.local_release_gate import compatibility_scope

        result = compatibility_scope({
            "status": "DEFERRED_TO_TARGET_RUNTIME",
            "PYTHON36_COMPAT": "DEFERRED_TO_TARGET_RUNTIME",
            "MARIADB55_COMPAT": "NOT_APPLICABLE",
        })
        self.assertTrue(result["isolated_candidate_staging_allowed"])
        self.assertFalse(result["publication_allowed"])
        self.assertFalse(result["cutover_allowed"])
        self.assertEqual(
            result["required_next_step"],
            "READY_FOR_MANUAL_VFOS_COMPATIBILITY_PREFLIGHT",
        )

    def test_local_release_gate_removes_hosted_credentials_from_child_processes(self):
        from scripts.release.local_release_gate import _clean_env

        with mock.patch.dict(
                os.environ,
                {"GITHUB_TOKEN": "secret", "GH_TOKEN": "secret"},
                clear=False):
            environment = _clean_env()
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("GH_TOKEN", environment)

    def test_security_scan_does_not_treat_encoded_operator_payload_as_shell(self):
        from scripts.release.local_release_gate import _security_scan

        with tempfile.TemporaryDirectory(prefix="operator-security-scan-") as root:
            operator = os.path.join(root, "offline_operator.txt")
            encoded = base64.b64encode(b"scp should not be executed").decode("ascii")
            with open(operator, "w", encoding="ascii") as stream:
                stream.write("#!/usr/bin/env bash\n")
                stream.write("echo safe\n")
                stream.write("__SOURCE_BUNDLE_BEGIN__\n")
                stream.write(encoded + "\n")
                stream.write("scp\n")
                stream.write("__SOURCE_BUNDLE_END__\n")
            result = _security_scan(os.getcwd(), operator)
            self.assertEqual(result["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
