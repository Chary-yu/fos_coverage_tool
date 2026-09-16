import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


class LocalGateNoGithubTest(unittest.TestCase):
    def test_local_gate_passes_without_github_tokens_api_or_gh(self):
        from scripts.release.local_release_gate import (
            run_gate, verify_evidence_manifest,
        )

        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
        source_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo_root
        ).decode("ascii").strip()
        environment = dict(os.environ)
        for name in (
                "GITHUB_TOKEN", "GH_TOKEN", "ACTIONS_RUNTIME_TOKEN",
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
                "ACTIONS_ID_TOKEN_REQUEST_URL"):
            environment.pop(name, None)
        with tempfile.TemporaryDirectory(prefix="local-gate-no-github-") as root:
            clean_repo = os.path.join(root, "repo")
            subprocess.check_call(
                ["git", "clone", "-q", "--no-hardlinks", repo_root, clean_repo]
            )
            output = os.path.join(root, "evidence")
            with mock.patch.dict(os.environ, environment, clear=True):
                self.assertIsNone(shutil.which("gh"))
                self.assertNotIn("GITHUB_TOKEN", os.environ)
                self.assertNotIn("GH_TOKEN", os.environ)
                result = run_gate(
                    clean_repo, source_sha, output,
                    session_id="no-github-local-gate",
                    baseline_sha=source_sha,
                    test_modules=("tests.release.test_compatibility_surface",),
                )
            self.assertEqual(result["local_release_gate"], "PASS")
            self.assertEqual(result["compatibility"]["status"], "NOT_APPLICABLE")
            self.assertEqual(result["production_mutation"], "NONE")
            self.assertTrue(verify_evidence_manifest(output))


if __name__ == "__main__":
    unittest.main()
