"""Deterministic Git line mapping with no rename/similarity inference."""

from __future__ import absolute_import

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
        """Map text conservatively when no repository is available.

        Durable inheritance uses :meth:`map_git_file`; this helper exists for
        parser/unit-test callers that only provide two text snapshots.  It
        deliberately considers one changed region bounded by exact prefix and
        suffix lines.  It never searches the whole file for a repeated line,
        which keeps the fallback fail-closed as well.
        """
        old_lines = self._split_lines(old_text)
        new_lines = self._split_lines(new_text)
        mapping = {}
        ambiguous = set()
        deleted = set()
        added = set()
        prefix = 0
        while prefix < len(old_lines) and prefix < len(new_lines) and \
                old_lines[prefix] == new_lines[prefix]:
            mapping[prefix + 1] = prefix + 1
            prefix += 1
        suffix = 0
        while (suffix < len(old_lines) - prefix and
               suffix < len(new_lines) - prefix and
               old_lines[len(old_lines) - suffix - 1] ==
               new_lines[len(new_lines) - suffix - 1]):
            suffix += 1
        for index in range(suffix):
            old_line = len(old_lines) - index
            new_line = len(new_lines) - index
            mapping[old_line] = new_line
        self._map_hunk(
            old_lines[prefix:len(old_lines) - suffix if suffix else len(old_lines)],
            new_lines[prefix:len(new_lines) - suffix if suffix else len(new_lines)],
            prefix + 1, prefix + 1, mapping, ambiguous, deleted, added,
        )
        return LineMapping(mapping, ambiguous, deleted, added)

    def map_git_file(self, repo_path, old_commit, new_commit, relative_path,
                     timeout=30):
        if not relative_path or os.path.isabs(str(relative_path)) or ".." in str(relative_path).split("/"):
            raise ValueError("Git line-map path is not repository-relative")
        old_text = self._show(repo_path, old_commit, relative_path, timeout)
        new_text = self._show(repo_path, new_commit, relative_path, timeout)
        return self.map_git_text(
            repo_path, old_commit, new_commit, relative_path,
            old_text, new_text, timeout=timeout,
        )

    def map_git_text(self, repo_path, old_commit, new_commit, relative_path,
                     old_text, new_text, timeout=30):
        """Map already-read Git snapshots using one diff invocation."""
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
        old_lines = self._split_lines(old_text)
        new_lines = self._split_lines(new_text)
        mapping = {}
        ambiguous = set()
        deleted = set()
        added = set()
        hunks = []
        for match in re.finditer(
                r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@",
                diff, re.MULTILINE):
            old_start = int(match.group(1))
            old_count = int(match.group(2) or 1)
            new_start = int(match.group(3))
            new_count = int(match.group(4) or 1)
            # For a zero-length hunk Git reports the insertion/deletion point
            # rather than a physical line.  Convert it to the first line
            # after that point so the half-open ranges below remain uniform.
            old_segment_start = old_start if old_count else old_start + 1
            new_segment_start = new_start if new_count else new_start + 1
            hunks.append((old_segment_start, old_count,
                          new_segment_start, new_count))

        old_cursor = 1
        new_cursor = 1
        for old_start, old_count, new_start, new_count in hunks:
            old_gap = old_start - old_cursor
            new_gap = new_start - new_cursor
            if old_gap == new_gap and old_gap >= 0:
                for offset in range(old_gap):
                    mapping[old_cursor + offset] = new_cursor + offset
            old_segment = old_lines[old_start - 1:old_start - 1 + old_count]
            new_segment = new_lines[new_start - 1:new_start - 1 + new_count]
            self._map_hunk(
                old_segment, new_segment, old_start, new_start,
                mapping, ambiguous, deleted, added,
            )
            old_cursor = old_start + old_count
            new_cursor = new_start + new_count

        old_gap = len(old_lines) + 1 - old_cursor
        new_gap = len(new_lines) + 1 - new_cursor
        if old_gap == new_gap and old_gap >= 0:
            for offset in range(old_gap):
                mapping[old_cursor + offset] = new_cursor + offset
        result = LineMapping(mapping, ambiguous, deleted, added)
        result.fingerprint = hashlib.sha256((diff + repr(result.to_dict())).encode(
            "utf-8"
        )).hexdigest()
        return result

    @staticmethod
    def _split_lines(value):
        return str(value or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()

    def _map_hunk(self, old_lines, new_lines, old_start, new_start,
                  mapping, ambiguous, deleted, added):
        """Apply only local, unique normalized recovery within one hunk."""
        old_positions = {}
        new_positions = {}
        for offset, line in enumerate(old_lines):
            old_positions.setdefault(self._line_tokens(line), []).append(offset)
        for offset, line in enumerate(new_lines):
            new_positions.setdefault(self._line_tokens(line), []).append(offset)

        pairs = []
        for key, old_offsets in old_positions.items():
            new_offsets = new_positions.get(key) or []
            if len(old_offsets) == 1 and len(new_offsets) == 1:
                old_offset, new_offset = old_offsets[0], new_offsets[0]
                # A pair inside a changed hunk is recoverable only when the
                # source spelling changed while normalized tokens stayed the
                # same.  Exact-text delete/re-add is a new physical line and
                # must remain permanently unlinked (R76).
                if old_lines[old_offset] != new_lines[new_offset]:
                    pairs.append((old_offset, new_offset, key))
        pairs.sort()
        if any(left >= right for (left, _, _), (right, _, _) in zip(pairs, pairs[1:])) or \
                any(left >= right for (_, left, _), (_, right, _) in zip(pairs, pairs[1:])):
            pairs = []

        matched_old = {item[0] for item in pairs}
        matched_new = {item[1] for item in pairs}
        # A hunk containing another real-token change is not a formatting-only
        # recovery opportunity.  This prevents a nearby unchanged line from
        # being used as a similarity anchor for an unrelated edit.
        if any(self._line_tokens(line) and offset not in matched_old
               for offset, line in enumerate(old_lines)) or \
                any(self._line_tokens(line) and offset not in matched_new
                    for offset, line in enumerate(new_lines)):
            pairs = []
            matched_old = set()
            matched_new = set()

        for old_offset, new_offset, _ in pairs:
            old_line = old_start + old_offset
            new_line = new_start + new_offset
            mapping[old_line] = new_line

        for offset in range(len(old_lines)):
            line_number = old_start + offset
            if offset not in matched_old:
                deleted.add(line_number)
                if new_lines:
                    ambiguous.add(line_number)
        for offset in range(len(new_lines)):
            if offset not in matched_new:
                added.add(new_start + offset)

    def _line_tokens(self, line):
        return tuple(self.lexer.tokenize(line))

    @staticmethod
    def _show(repo_path, commit, relative_path, timeout):
        return subprocess.check_output([
            "git", "-C", os.path.realpath(repo_path), "show",
            "{}:{}".format(commit, relative_path),
        ], stderr=subprocess.STDOUT, timeout=float(timeout), universal_newlines=True)
