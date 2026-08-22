import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))


class TestRealBrowserEvidence(unittest.TestCase):
    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        self.script = os.path.join(
            ROOT, "scripts", "diagnostics", "real_browser_evidence.js"
        )

    def test_help_describes_exact_revision_and_separate_gate_outputs(self):
        result = subprocess.run(
            ["node", self.script, "--help"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        output = result.stdout.decode("utf-8")
        self.assertIn("--expected-revision", output)
        self.assertIn("--browser-evidence-output", output)
        self.assertIn("real HTTP Candidate page", output)

    def test_unreachable_target_is_incomplete_and_header_is_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = os.path.join(directory, "workload.json")
            evidence_path = os.path.join(directory, "performance.json")
            browser_path = os.path.join(directory, "browser.json")
            secret = "do-not-write-this-secret"
            result = subprocess.run(
                [
                    "node", self.script,
                    "--url", "http://127.0.0.1:1/not-running.gcov.html",
                    "--expected-revision", "a" * 40,
                    "--header", "X-Remote-User=" + secret,
                    "--output", report_path,
                    "--evidence-output", evidence_path,
                    "--browser-evidence-output", browser_path,
                    "--timeout-ms", "3000",
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            with open(report_path, "r", encoding="utf-8") as stream:
                report = json.load(stream)
            with open(evidence_path, "r", encoding="utf-8") as stream:
                evidence = json.load(stream)
            with open(browser_path, "r", encoding="utf-8") as stream:
                browser = json.load(stream)
            self.assertEqual(report["status"], "INCOMPLETE")
            self.assertFalse(report["synthetic"])
            self.assertEqual(evidence["status"], "INCOMPLETE")
            self.assertEqual(browser["status"], "INCOMPLETE")
            self.assertNotIn(secret, json.dumps(report))
            self.assertNotIn(secret, json.dumps(evidence))
            self.assertNotIn(secret, json.dumps(browser))
            with open(report_path, "rb") as stream:
                report_sha = hashlib.sha256(stream.read()).hexdigest()
            self.assertEqual(evidence["artifact_sha256"], report_sha)
            self.assertEqual(browser["artifact_sha256"], report_sha)

    def test_local_fixture_mode_is_explicitly_synthetic_and_not_release_eligible(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = os.path.join(directory, "workload.json")
            evidence_path = os.path.join(directory, "performance.json")
            result = subprocess.run(
                [
                    "node", self.script,
                    "--url", "http://127.0.0.1:1/not-running.gcov.html",
                    "--expected-revision", "a" * 40,
                    "--allow-local-fixture",
                    "--output", report_path,
                    "--evidence-output", evidence_path,
                    "--timeout-ms", "3000",
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            with open(report_path, "r", encoding="utf-8") as stream:
                report = json.load(stream)
            with open(evidence_path, "r", encoding="utf-8") as stream:
                evidence = json.load(stream)
            self.assertTrue(report["synthetic"])
            self.assertFalse(report["release_eligible"])
            self.assertEqual(report["evidence_class"], "real_http_chromium_fixture")
            self.assertTrue(evidence["synthetic"])
            self.assertFalse(evidence["release_eligible"])


if __name__ == "__main__":
    unittest.main()
