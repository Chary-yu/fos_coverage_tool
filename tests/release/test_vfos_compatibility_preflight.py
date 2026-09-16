import os
import subprocess
import sys
import tempfile
import unittest


REPOSITORY_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
GENERATOR = os.path.join(
    REPOSITORY_ROOT,
    "scripts",
    "release",
    "generate_vfos_compatibility_preflight.py",
)
SOURCE_SHA = "b9a21aae42b69bdbc8d7596a7b661e350febdbba"
SOURCE_TREE = "a46fcadc4576deea25ecaf5801cac1a5fd8e9484"


class VfosCompatibilityPreflightGeneratorTests(unittest.TestCase):
    def generate(self, output_path):
        return subprocess.run(
            [
                sys.executable,
                GENERATOR,
                "--source-sha",
                SOURCE_SHA,
                "--source-tree",
                SOURCE_TREE,
                "--expected-host",
                "vfoswind",
                "--output",
                output_path,
            ],
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )

    def test_generator_emits_standalone_read_only_preflight(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, "preflight.txt")
            result = self.generate(output_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with open(output_path, "r") as handle:
                content = handle.read()

            self.assertTrue(content.startswith("#!/usr/bin/env bash\n"))
            self.assertIn("EXPECTED_SOURCE_SHA=" + SOURCE_SHA, content)
            self.assertIn("EXPECTED_SOURCE_TREE=" + SOURCE_TREE, content)
            self.assertIn("EXPECTED_HOST=vfoswind", content)
            self.assertIn('record "PYTHON36_COMPAT" "FAIL"', content)
            self.assertIn(
                'record "READY_FOR_ISOLATED_CANDIDATE_STAGING" "NO"',
                content,
            )
            self.assertIn('record "READY_FOR_PUBLICATION" "NO"', content)
            self.assertIn('record "READY_FOR_CUTOVER" "NO"', content)
            self.assertIn('record "PRODUCTION_MUTATION" "NONE"', content)
            self.assertNotIn("systemctl start", content)
            self.assertNotIn("systemctl stop", content)
            self.assertNotIn("systemctl restart", content)
            self.assertNotIn("curl ", content)
            self.assertNotIn("wget ", content)
            self.assertNotIn("ssh ", content)

            syntax = subprocess.run(
                ["bash", "-n", output_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
            self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_missing_python36_fails_closed_before_any_staging(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, "preflight.txt")
            result = self.generate(output_path)
            self.assertEqual(result.returncode, 0, result.stderr)

            source_root = os.path.join(temp_dir, "source")
            os.makedirs(source_root)
            environment = os.environ.copy()
            environment["FOS_VFOS_PYTHON36_BIN"] = "python3.6-does-not-exist"

            run = subprocess.run(
                [
                    "bash",
                    output_path,
                    "--source-root",
                    source_root,
                    "--candidate-root",
                    os.path.join(temp_dir, "candidate"),
                ],
                cwd=REPOSITORY_ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )

            self.assertNotEqual(run.returncode, 0)
            self.assertIn("SOURCE_ROOT=" + source_root, run.stdout)
            self.assertIn(
                "CANDIDATE_ROOT=" + os.path.join(temp_dir, "candidate"),
                run.stdout,
            )
            self.assertIn("PYTHON36_COMPAT=FAIL", run.stdout)
            self.assertIn("READY_FOR_ISOLATED_CANDIDATE_STAGING=NO", run.stdout)
            self.assertIn("READY_FOR_PUBLICATION=NO", run.stdout)
            self.assertIn("READY_FOR_CUTOVER=NO", run.stdout)
            self.assertIn("PRODUCTION_MUTATION=NONE", run.stdout)

    def test_generator_help_is_local_and_deterministic(self):
        result = subprocess.run(
            [sys.executable, GENERATOR, "--help"],
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--source-sha", result.stdout)
        self.assertIn("--source-tree", result.stdout)
        self.assertIn("--output", result.stdout)


if __name__ == "__main__":
    unittest.main()
