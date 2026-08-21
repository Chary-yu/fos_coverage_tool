"""Deterministic Git line mapping with no rename/similarity inference."""

from __future__ import absolute_import

import difflib
import hashlib
import os
import re
import subprocess

from app.inheritance.normalizer import CppLexer


class LineMapping(object):
    def __init__(self, mapping=None, ambiguous=None, deleted=None, added=None,
                 fingerprint=""):
        self.mapping = dict(mapping or {})
        self.ambiguous = set(ambiguous or [])
        self.deleted = set(deleted or [])
        self.added = set(added or [])
        self.fingerprint = fingerprint or hashlib.sha256(repr(sorted(
            self.mapping.items())).encode("utf-8")).hexdigest()

    def get(self, old_line):
        if int(old_line) in self.ambiguous or int(old_line) in self.deleted:
            return None
        return self.mapping.get(int(old_line))

    def to_dict(self):
        return {
            "mapping": dict(sorted(self.mapping.items())),
            "ambiguous": sorted(self.ambiguous),
            "deleted": sorted(self.deleted),
            "added": sorted(self.added),
            "fingerprint": self.fingerprint,
        }


class GitLineMapEngine(object):
    def __init__(self, lexer=None):
        self.lexer = lexer or CppLexer()

    def map_text(self, old_text, new_text):
        old_lines = str(old_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        new_lines = str(new_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        old_tokens = [tuple(self.lexer.tokenize(line)) for line in old_lines]
        new_tokens = [tuple(self.lexer.tokenize(line)) for line in new_lines]
        matcher = difflib.SequenceMatcher(a=old_tokens, b=new_tokens, autojunk=False)
        mapping = {}
        ambiguous = set()
        deleted = set()
        added = set()
        for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            old_numbers = range(old_start + 1, old_end + 1)
            new_numbers = range(new_start + 1, new_end + 1)
            if tag == "equal":
                for old_line, new_line in zip(old_numbers, new_numbers):
                    mapping[old_line] = new_line
            elif tag == "replace":
                if old_end - old_start == 1 and new_end - new_start == 1 and (
                        old_tokens[old_start] == new_tokens[new_start]):
                    mapping[old_start + 1] = new_start + 1
                else:
                    ambiguous.update(old_numbers)
                    deleted.update(old_numbers)
                    added.update(new_numbers)
            elif tag == "delete":
                deleted.update(old_numbers)
            elif tag == "insert":
                added.update(new_numbers)
        return LineMapping(mapping, ambiguous, deleted, added)

    def map_git_file(self, repo_path, old_commit, new_commit, relative_path,
                     timeout=30):
        if not relative_path or os.path.isabs(str(relative_path)) or ".." in str(relative_path).split("/"):
            raise ValueError("Git line-map path is not repository-relative")
        old_text = self._show(repo_path, old_commit, relative_path, timeout)
        new_text = self._show(repo_path, new_commit, relative_path, timeout)
        # Verify the primary Git evidence even when the pure mapper is used to
        # produce the physical mapping.  Rename detection and external diff
        # drivers are explicitly disabled.
        diff = subprocess.check_output([
            "git", "-C", os.path.realpath(repo_path), "diff", "--no-ext-diff",
            "--no-renames", "--unified=0", str(old_commit), str(new_commit),
            "--", str(relative_path),
        ], stderr=subprocess.STDOUT, timeout=float(timeout), universal_newlines=True)
        if "rename from " in diff or "similarity index " in diff:
            return LineMapping(deleted=range(1, len(old_text.splitlines()) + 1),
                               added=range(1, len(new_text.splitlines()) + 1))
        result = self.map_text(old_text, new_text)
        result.fingerprint = hashlib.sha256((diff + repr(result.to_dict())).encode(
            "utf-8"
        )).hexdigest()
        return result

    @staticmethod
    def _show(repo_path, commit, relative_path, timeout):
        return subprocess.check_output([
            "git", "-C", os.path.realpath(repo_path), "show",
            "{}:{}".format(commit, relative_path),
        ], stderr=subprocess.STDOUT, timeout=float(timeout), universal_newlines=True)
