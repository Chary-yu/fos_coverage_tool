"""Target-commit Git blame parser with boundary metadata support."""

import os
import subprocess


def parse_porcelain(text):
    records = []
    current = None
    pending_boundary = False
    for line in text.splitlines():
        if line.startswith("boundary"):
            pending_boundary = True
            if current is not None:
                current["boundary"] = True
            continue
        if line.startswith("filename "):
            if current is not None:
                current["filename"] = line[9:]
                current["boundary"] = bool(current.get("boundary") or pending_boundary)
                records.append(current)
                current = None
            pending_boundary = False
            continue
        if line.startswith("\t"):
            if current is not None:
                current["line_text"] = line[1:]
            continue
        fields = line.split()
        if len(fields) >= 3 and _looks_like_sha(fields[0]):
            if current is not None:
                records.append(current)
            current = {
                "commit": fields[0].lstrip("^"),
                "original_line": int(fields[1]),
                "final_line": int(fields[2]),
                "boundary": fields[0].startswith("^"),
                "author": "",
                "author_mail": "",
                "author_time": "",
                "author_tz": "",
                "filename": "",
                "line_text": "",
            }
            continue
        if current is None:
            continue
        if line.startswith("author "):
            current["author"] = line[7:]
        elif line.startswith("author-mail "):
            current["author_mail"] = line[12:].strip("<>")
        elif line.startswith("author-time "):
            current["author_time"] = line[12:]
        elif line.startswith("author-tz "):
            current["author_tz"] = line[10:]
    if current is not None:
        records.append(current)
    return records


def _looks_like_sha(value):
    value = value.lstrip("^")
    return len(value) in (7, 8, 40) and all(char in "0123456789abcdef" for char in value.lower())


def blame_file(repo_path, commit_sha, file_path):
    if not commit_sha or not file_path or os.path.isabs(file_path) or ".." in file_path.replace("\\", "/").split("/"):
        raise ValueError("unsafe blame identity")
    result = subprocess.run(
        ["git", "-C", repo_path, "blame", "--line-porcelain", commit_sha, "--", file_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
    return parse_porcelain(result.stdout.decode("utf-8", errors="replace"))


def owner_by_line(repo_path, commit_sha, file_path):
    records = blame_file(repo_path, commit_sha, file_path)
    return {int(record["final_line"]): record for record in records}
