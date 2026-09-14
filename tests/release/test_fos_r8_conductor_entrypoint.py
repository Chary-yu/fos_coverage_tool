import os
import subprocess
import sys
import tempfile
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
CONDUCTOR = os.path.join(ROOT, "scripts", "release", "fos_r8_conductor.py")


class R8ConductorEntrypointRegressionTest(unittest.TestCase):
    def test_direct_execution_from_outside_repo_resolves_scripts_package(self):
        with tempfile.TemporaryDirectory(prefix="r8-conductor-entry-") as root:
            repo = os.path.join(root, "repo")
            release = os.path.join(repo, "scripts", "release")
            upgrade = os.path.join(repo, "scripts", "upgrade")
            os.makedirs(release)
            os.makedirs(upgrade)

            with open(CONDUCTOR, "r", encoding="utf-8") as stream:
                source = stream.read()
            with open(
                os.path.join(release, "fos_r8_conductor.py"),
                "w",
                encoding="utf-8",
            ) as stream:
                stream.write(source)
            with open(
                os.path.join(release, "fos_r8_conductor_base.py"),
                "w",
                encoding="utf-8",
            ) as stream:
                stream.write("import json\n")
            with open(
                os.path.join(release, "vfoswind_attempt_config.py"),
                "w",
                encoding="utf-8",
            ) as stream:
                stream.write(
                    "def normalize_vfoswind_attempt_config(config, "
                    "candidate_application_root=''):\n    return config\n"
                )
            with open(
                os.path.join(upgrade, "evidence_manifest.py"),
                "w",
                encoding="utf-8",
            ) as stream:
                stream.write("MANIFEST_FILENAME = 'production_evidence_manifest.json'\n")

            result = subprocess.run(
                [sys.executable, os.path.join(release, "fos_r8_conductor.py"), "--help"],
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("usage: fos_r8_conductor.py", result.stdout)


if __name__ == "__main__":
    unittest.main()
