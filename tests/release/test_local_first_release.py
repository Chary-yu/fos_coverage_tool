import json
import os
import tempfile
import unittest


class LocalFirstReleaseTest(unittest.TestCase):
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
        rendered = render_contract(contract)
        self.assertIn("PRODUCTION_RELEASE_AUTHORITY=OFFLINE_OPERATOR_ONLY", rendered)
        self.assertIn("GITHUB_SOURCE_REPOSITORY_ONLY=YES", rendered)

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


if __name__ == "__main__":
    unittest.main()
