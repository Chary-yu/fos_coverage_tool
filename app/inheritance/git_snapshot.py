"""Git ancestry and immutable source snapshot provider."""

from __future__ import absolute_import

import os
import subprocess


SOURCE_EXTENSIONS = (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx")


class GitTechnicalFailure(RuntimeError):
    pass


class GitSnapshotProvider(object):
    def __init__(self, repo_path, timeout=30, fetch_remote=None):
        self.repo_path = os.path.realpath(str(repo_path or ""))
        self.timeout = float(timeout)
        self.fetch_remote = fetch_remote

    def _run(self, args, check=True):
        try:
            return subprocess.check_output(
                ["git", "-C", self.repo_path] + list(args),
                stderr=subprocess.STDOUT, timeout=self.timeout,
                universal_newlines=True,
            ).strip()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            if check:
                raise GitTechnicalFailure(str(exc))
            return ""

    def commit_available(self, commit):
        try:
            subprocess.check_call([
                "git", "-C", self.repo_path, "cat-file", "-e",
                "{}^{{commit}}".format(commit),
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=self.timeout)
            return True
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False

    def ensure_commit(self, commit):
        if self.commit_available(commit):
            return True
        if self.fetch_remote:
            self._run(["fetch", "--no-tags", str(self.fetch_remote), str(commit)])
        if not self.commit_available(commit):
            raise GitTechnicalFailure("required commit is unavailable: {}".format(commit))
        return True

    def is_ancestor(self, old_commit, new_commit):
        self.ensure_commit(old_commit)
        self.ensure_commit(new_commit)
        if old_commit == new_commit:
            return True
        try:
            subprocess.check_call([
                "git", "-C", self.repo_path, "merge-base", "--is-ancestor",
                str(old_commit), str(new_commit),
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=self.timeout)
            return True
        except subprocess.CalledProcessError:
            return False
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitTechnicalFailure(str(exc))

    def branch(self, commit):
        return self._run(["show", "-s", "--format=%D", str(commit)], check=False)

    def read_file(self, commit, relative_path):
        if os.path.isabs(str(relative_path)) or ".." in str(relative_path).replace("\\", "/").split("/"):
            raise ValueError("source path must be repository-relative")
        return self._run(["show", "{}:{}".format(commit, relative_path)])

    def list_source_files(self, commit):
        """List the repository C/C++ universe for dependency resolution."""
        output = self._run(["ls-tree", "-r", "--name-only", str(commit)])
        paths = []
        for value in output.splitlines():
            path = value.strip().replace("\\", "/")
            if (not path or os.path.isabs(path) or
                    ".." in path.split("/") or
                    not path.lower().endswith(SOURCE_EXTENSIONS)):
                continue
            paths.append(path)
        return sorted(set(paths))
