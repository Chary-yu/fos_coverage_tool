from __future__ import absolute_import

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from app.incremental.blame import blame_file
from app.incremental.git_diff import changed_files, verify_commit
from app.inheritance.git_snapshot import GitSnapshotProvider
from app.inheritance.line_map import GitLineMapEngine
from app.git_runtime_compat import install
from scripts.diagnostics.production_inventory import _repo_observation
from scripts.upgrade.run_verified_backup_rehearsal import _revision


class OldGitRuntimeCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.real_git = os.environ.get("FOS_REAL_GIT") or shutil.which("git")
        if not self.real_git:
            self.skipTest("git is unavailable")
        self.root = tempfile.mkdtemp(prefix="fos-old-git-")
        self.repo = os.path.join(self.root, "repo")
        os.makedirs(self.repo)
        self._git(["init", "-q"], self.repo)
        self._git(["config", "user.name", "Old Git Test"], self.repo)
        self._git(["config", "user.email", "old-git@example.invalid"], self.repo)
        with open(os.path.join(self.repo, "sample.c"), "w") as stream:
            stream.write("int kept = 1;\nint changed = 1;\n")
        self._git(["add", "sample.c"], self.repo)
        self._git(["commit", "-qm", "old"], self.repo)
        self.old = self._output(["rev-parse", "HEAD"], self.repo).strip()
        with open(os.path.join(self.repo, "sample.c"), "w") as stream:
            stream.write("int kept = 1;\nint changed = 2;\n")
        self._git(["add", "sample.c"], self.repo)
        self._git(["commit", "-qm", "new"], self.repo)
        self.new = self._output(["rev-parse", "HEAD"], self.repo).strip()

        self.bin_dir = os.path.join(self.root, "bin")
        os.makedirs(self.bin_dir)
        self.old_git = os.path.join(self.bin_dir, "old-git")
        with open(self.old_git, "w") as stream:
            stream.write("#!/bin/sh\n")
            stream.write("for arg in \"$@\"; do\n")
            stream.write("  if [ \"$arg\" = \"-C\" ]; then\n")
            stream.write("    echo 'old git: unknown option -C' >&2\n")
            stream.write("    exit 129\n")
            stream.write("  fi\n")
            stream.write("done\n")
            stream.write("exec \"{}\" \"$@\"\n".format(self.real_git))
        os.chmod(self.old_git, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

        self.repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
        self.shim_dir = os.path.join(self.repo_root, "scripts", "compat")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _git(self, args, cwd):
        subprocess.check_call(
            [self.real_git] + list(args), cwd=cwd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def _output(self, args, cwd):
        return subprocess.check_output(
            [self.real_git] + list(args), cwd=cwd,
            stderr=subprocess.DEVNULL, universal_newlines=True,
        )

    def _old_git_environment(self):
        path = self.shim_dir + os.pathsep + os.environ.get("PATH", "")
        return {
            "PATH": path,
            "FOS_REAL_GIT": self.old_git,
            "FOS_GIT_COMPAT_INSTALLED": "1",
        }

    def test_install_prepends_adapter_without_changing_real_git_identity(self):
        original_path = os.environ.get("PATH", "")
        with mock.patch.dict(os.environ, {
                "PATH": original_path,
                "FOS_GIT_COMPAT_INSTALLED": "",
                "FOS_REAL_GIT": "",
        }, clear=False):
            self.assertTrue(install())
            self.assertEqual(os.path.realpath(self.real_git), os.environ["FOS_REAL_GIT"])
            self.assertEqual(
                os.path.realpath(self.shim_dir),
                os.path.realpath(os.environ["PATH"].split(os.pathsep)[0]),
            )
            self.assertFalse(install())

    def test_runtime_git_paths_work_when_real_git_rejects_dash_c(self):
        with mock.patch.dict(os.environ, self._old_git_environment(), clear=False):
            # The checked-in application code still requests Git's modern -C
            # form.  PATH resolves the request through the compatibility shim,
            # which removes -C before the deliberately old-Git-like executable.
            self.assertTrue(verify_commit(self.repo, self.old))
            self.assertTrue(verify_commit(self.repo, self.new))
            self.assertEqual(
                [{"status": "M", "paths": ["sample.c"]}],
                changed_files(self.repo, self.old, self.new),
            )
            blame = blame_file(self.repo, self.new, "sample.c")
            self.assertEqual([1, 2], [item["final_line"] for item in blame])
            mapping = GitLineMapEngine().map_git_file(
                self.repo, self.old, self.new, "sample.c"
            )
            self.assertEqual(1, mapping.get(1))
            self.assertIsNone(mapping.get(2))
            provider = GitSnapshotProvider(self.repo)
            self.assertTrue(provider.commit_available(self.old))
            self.assertTrue(provider.is_ancestor(self.old, self.new))
            self.assertIn("int changed = 2;", provider.read_file(self.new, "sample.c"))
            batch = dict(provider.read_files(self.new, ["sample.c"]))
            self.assertIn("int kept = 1;", batch["sample.c"])
            self.assertEqual(self.new, _revision(self.repo))
            inventory = _repo_observation(self.repo)
            self.assertEqual("PASSED", inventory["status"])
            self.assertEqual(self.new, inventory["head"])

    def test_adapter_fails_closed_for_invalid_dash_c(self):
        env = os.environ.copy()
        env.update(self._old_git_environment())
        result = subprocess.run(
            [os.path.join(self.shim_dir, "git"), "-C", self.repo],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        self.assertEqual(129, result.returncode)


if __name__ == "__main__":
    unittest.main()
