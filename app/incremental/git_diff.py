"""Real Git diff extraction for the immutable oldgit..newgit range."""

import os
import re
import subprocess


HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _git(repo_path, args):
    command = ["git", "-C", repo_path] + list(args)
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            check=False)
    if result.returncode:
        raise RuntimeError("git command failed: {}".format(
            result.stderr.decode("utf-8", errors="replace").strip()
        ))
    return result.stdout.decode("utf-8", errors="replace")


def verify_commit(repo_path, commit_sha):
    if not commit_sha:
        return False
    result = subprocess.run(
        ["git", "-C", repo_path, "cat-file", "-e", "{}^{{commit}}".format(commit_sha)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    return result.returncode == 0


def parse_unified_diff(diff_text):
    changes = {}
    current_path = None
    new_line = None
    hunk_start = None
    hunk_count = 0
    hunk_has_body = False

    def finalize_hunk():
        if current_path and hunk_start is not None and not hunk_has_body and hunk_count:
            changes.setdefault(current_path, []).extend(
                range(hunk_start, hunk_start + hunk_count)
            )

    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_path = line[6:]
            changes.setdefault(current_path, [])
            new_line = None
            hunk_start = None
            hunk_count = 0
            hunk_has_body = False
            continue
        if line.startswith("+++ /dev/null"):
            finalize_hunk()
            current_path = None
            continue
        if not current_path:
            continue
        match = HUNK_RE.search(line)
        if match:
            finalize_hunk()
            hunk_start = int(match.group(1))
            hunk_count = int(match.group(2) or 1)
            new_line = hunk_start
            hunk_has_body = False
            continue
        if new_line is None or line.startswith(chr(92) + " No newline"):
            continue
        hunk_has_body = True
        if line.startswith("+") and not line.startswith("+++"):
            changes[current_path].append(new_line)
            new_line += 1
        elif line.startswith("-"):
            continue
        else:
            new_line += 1
    finalize_hunk()
    return {path: sorted(set(lines)) for path, lines in changes.items()}


def added_lines(repo_path, oldgit, newgit):
    if not verify_commit(repo_path, oldgit) or not verify_commit(repo_path, newgit):
        raise ValueError("oldgit and newgit must both resolve to commits")
    diff = _git(repo_path, [
        "diff", "--no-ext-diff", "--unified=0", oldgit, newgit, "--",
    ])
    return parse_unified_diff(diff)


def changed_files(repo_path, oldgit, newgit):
    if not verify_commit(repo_path, oldgit) or not verify_commit(repo_path, newgit):
        raise ValueError("oldgit and newgit must both resolve to commits")
    output = _git(repo_path, ["diff", "--name-status", oldgit, newgit, "--"])
    result = []
    for line in output.splitlines():
        fields = line.split("\t")
        if not fields:
            continue
        status = fields[0]
        paths = fields[1:]
        result.append({"status": status, "paths": paths})
    return result
