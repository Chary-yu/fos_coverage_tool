#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Git 增量代码覆盖率计算。

此模块既可以单独执行，也可以被 ``enhance_coverage.py incremental`` 调用。
它根据 Git diff 的新增行和 LCOV ``.info`` 中的 DA 记录，区分：已覆盖、
未覆盖、无需覆盖（例如空行/注释）以及覆盖信息缺失的文件。
"""

import argparse
import html as html_lib
import io
import json
import os
import re
import subprocess
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from app.incremental.service import IncrementalService


STATUS_COVERED = "已覆盖"
STATUS_UNCOVERED = "未覆盖"
STATUS_IGNORED = "无需覆盖"
STATUS_MISSING = "覆盖信息缺失"

_PATH_INDEX_CACHE = {}
MAX_BLAME_RANGES_PER_FILE = 24


def normalize_path(path):
    """Return a platform-independent normalized path without a leading ``./``."""
    normalized = os.path.normpath(str(path or "")).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


VALID_SOURCE_EXTENSIONS = ('.c', '.h', '.cc', '.cpp', '.cxx', '.hh', '.hpp', '.hxx', '.inl')

def is_valid_source_file(file_path):
    """Return True if the file path ends with C/C++ source/header extension (case-insensitive)."""
    if not file_path:
        return False
    lower_path = str(file_path).lower()
    return lower_path.endswith(VALID_SOURCE_EXTENSIONS)


def run_git_diff(repo_path, oldgit, newgit):
    """Return the textual diff between two revisions without invoking a shell.

    ``text=`` on ``subprocess.run`` was introduced in Python 3.7.  Keep this
    byte-oriented form so the incremental command works on Python 3.6.8 too.
    """
    proc = subprocess.Popen(
        ["git", "diff", "--no-ext-diff", "--no-color", oldgit, newgit],
        cwd=repo_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = proc.communicate()
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    if proc.returncode != 0:
        message = stderr.strip() or "git diff failed"
        raise RuntimeError("git diff {} {} failed: {}".format(oldgit, newgit, message))
    return stdout


def run_git_developer_file_changes(repo_path, oldgit, newgit, repository_name=""):
    """Return author-to-file changes for commits in ``oldgit..newgit``.

    Git author information is deliberately collected from commit history instead
    of the local operating-system account.  A file may be associated with more
    than one author when several commits touched it in the selected range.
    Deleted files are excluded because they cannot contain a current coverage
    review target.  Renames and copies are assigned to their destination path.
    """
    proc = subprocess.Popen(
        [
            "git", "log", "--no-ext-diff", "--no-color", "--find-renames",
            "--date=iso-strict", "--format=%H%x1f%an%x1f%ae%x1f%ad%x1f%s",
            "--name-status", "{}..{}".format(oldgit, newgit),
        ],
        cwd=repo_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = proc.communicate()
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    if proc.returncode != 0:
        message = stderr.strip() or "git log failed"
        raise RuntimeError(
            "git log {}..{} failed: {}".format(oldgit, newgit, message)
        )

    changes = []
    current_commit = None
    for raw_line in stdout.splitlines():
        if "\x1f" in raw_line:
            fields = raw_line.split("\x1f", 4)
            if len(fields) == 5 and re.match(r"^[0-9a-fA-F]{7,64}$", fields[0]):
                current_commit = {
                    "repository": repository_name,
                    "commit": fields[0],
                    "author_name": fields[1] or "Unknown",
                    "author_email": fields[2] or "",
                    "committed_at": fields[3] or "",
                    "subject": fields[4] or "",
                }
                continue
        if not current_commit or not raw_line:
            continue

        parts = raw_line.split("\t")
        status = parts[0] if parts else ""
        if not status or status.startswith("D"):
            continue
        # R100 old/path new/path and C100 old/path new/path use the target file.
        file_path = parts[-1] if len(parts) > 1 else ""
        file_path = normalize_path(file_path)
        if not file_path or not is_valid_source_file(file_path):
            continue
        item = dict(current_commit)
        item.update({"file_path": file_path, "change_type": status})
        changes.append(item)
    return changes


def generate_diff_files(repo_path, oldgit, newgit, out_file):
    """Generate a diff file. Kept for backward compatibility with the old CLI."""
    diff_text = run_git_diff(repo_path, oldgit, newgit)
    with open(out_file, "w", encoding="utf-8") as handle:
        handle.write(diff_text)
    print("[INFO] diff写入 {}".format(out_file))
    return diff_text


def parse_diff_text(diff_text):
    """Parse added lines from a unified diff into ``{file_path: [line_numbers]}``.

    Deleted files are skipped. Rename diffs use the target side reported by ``+++ b/``.
    """
    file_changes = {}
    current_file = None
    new_line_num = None

    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            candidate = line[4:].strip()
            if candidate == "/dev/null":
                current_file = None
            elif candidate.startswith("b/"):
                normalized = normalize_path(candidate[2:])
                current_file = normalized if is_valid_source_file(normalized) else None
            else:
                normalized = normalize_path(candidate)
                current_file = normalized if is_valid_source_file(normalized) else None
            if current_file:
                file_changes.setdefault(current_file, [])
            new_line_num = None
            continue

        if line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,\d+)?", line)
            new_line_num = int(match.group(1)) if match else None
            continue

        if current_file is None or new_line_num is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            file_changes[current_file].append(new_line_num)
            new_line_num += 1
        elif line.startswith("-") or line.startswith("\\ No newline"):
            continue
        else:
            new_line_num += 1

    return file_changes


def parse_diff(diff_file):
    """Parse added lines from an existing diff file."""
    with open(diff_file, "r", encoding="utf-8", errors="replace") as handle:
        return parse_diff_text(handle.read())


def _coalesce_line_ranges(line_numbers):
    """Return sorted contiguous ``(start, end)`` ranges for line numbers."""
    values = sorted(set(int(number) for number in (line_numbers or []) if int(number) > 0))
    if not values:
        return []

    ranges = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append((start, previous))
        start = previous = value
    ranges.append((start, previous))
    return ranges


_BLAME_HEADER_RE = re.compile(
    r"^(?P<boundary>\^?)(?P<commit>[0-9a-fA-F]{7,64})\s+"
    r"(?P<original>\d+)\s+(?P<final>\d+)(?:\s+(?P<count>\d+))?$"
)


def _format_blame_timestamp(timestamp, timezone_text=""):
    """Convert porcelain epoch metadata to a stable ISO-8601 string."""
    try:
        offset_text = str(timezone_text or "+0000")
        sign = -1 if offset_text.startswith("-") else 1
        digits = offset_text.lstrip("+-")
        hours = int(digits[:2] or 0)
        minutes = int(digits[2:4] or 0)
        tz = timezone(sign * timedelta(hours=hours, minutes=minutes))
        return datetime.fromtimestamp(int(timestamp), tz).strftime("%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError, OverflowError, OSError):
        return str(timestamp or "")


def parse_git_blame_porcelain(blame_text, selected_line_numbers=None):
    """Parse ``git blame --line-porcelain`` into final-line ownership.

    Porcelain emits a header and metadata followed by one source line.  A
    header may declare several consecutive lines; the final-line number is
    advanced for each emitted source line.  Boundary commits are represented
    by Git as ``^<sha>``; the caret is exposed separately while the public
    commit value remains the usable SHA.
    """
    selected = None if selected_line_numbers is None else set(
        int(number) for number in selected_line_numbers
    )
    result = {}
    current = None

    for raw_line in str(blame_text or "").splitlines():
        header = _BLAME_HEADER_RE.match(raw_line)
        if header:
            current = {
                "commit": header.group("commit"),
                "boundary": bool(header.group("boundary")),
                "final_line": int(header.group("final")),
                "line_count": int(header.group("count") or 1),
                "emitted": 0,
                "metadata": {},
            }
            continue

        if current is None:
            continue

        # A source line is the only porcelain line prefixed with a tab (Git's
        # documented form) or four spaces (some wrappers re-indent output).
        if raw_line.startswith("\t") or raw_line.startswith("    "):
            line_number = current["final_line"] + current["emitted"]
            metadata = current["metadata"]
            if selected is None or line_number in selected:
                email = str(metadata.get("author-mail", "") or "").strip()
                if email.startswith("<") and email.endswith(">"):
                    email = email[1:-1]
                result[line_number] = {
                    "author_name": str(metadata.get("author", "") or "").strip(),
                    "author_email": email,
                    "commit": current["commit"],
                    "boundary": current["boundary"],
                    "committed_at": _format_blame_timestamp(
                        metadata.get("author-time", ""),
                        metadata.get("author-tz", "+0000"),
                    ),
                    "subject": str(metadata.get("summary", "") or "").strip(),
                }
            current["emitted"] += 1
            if current["emitted"] >= current["line_count"]:
                current = None
            continue

        # Metadata values are allowed to contain spaces.  Unknown keys are
        # retained so future Git porcelain fields do not break parsing.
        if " " in raw_line:
            key, value = raw_line.split(" ", 1)
            current["metadata"][key] = value

    return result


def _run_git_blame(repo_path, newgit, file_path, start_line=None, end_line=None):
    command = ["git", "blame", "--line-porcelain"]
    if start_line is not None and end_line is not None:
        command.extend(["-L", "{},{}".format(start_line, end_line)])
    command.extend([newgit, "--", normalize_path(file_path)])
    proc = subprocess.Popen(
        command,
        cwd=repo_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = proc.communicate()
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    if proc.returncode != 0:
        message = stderr.strip() or "git blame failed"
        raise RuntimeError(
            "git blame {} {} failed for {}: {}".format(
                newgit, "-L {},{}".format(start_line, end_line)
                if start_line is not None else "whole-file", file_path, message
            )
        )
    return stdout


def run_git_line_authors(repo_path, newgit, file_changes, repository_name=""):
    """Attribute only final added lines to ``newgit`` using pinned blame.

    A file normally uses one blame process per coalesced range.  Highly
    fragmented additions use one whole-file process followed by an explicit
    line filter to avoid process explosion.  Missing requested lines are a
    hard error: production must not silently fall back to a file-level author.
    """
    result = {}
    for raw_file_path, raw_line_numbers in sorted((file_changes or {}).items()):
        file_path = normalize_path(raw_file_path)
        selected = set(int(number) for number in raw_line_numbers or [] if int(number) > 0)
        if not file_path or not selected:
            continue

        ranges = _coalesce_line_ranges(selected)
        if len(ranges) > MAX_BLAME_RANGES_PER_FILE:
            blame_text = _run_git_blame(repo_path, newgit, file_path)
            parsed = parse_git_blame_porcelain(blame_text, selected)
        else:
            parsed = {}
            for start_line, end_line in ranges:
                blame_text = _run_git_blame(
                    repo_path, newgit, file_path, start_line, end_line
                )
                parsed.update(parse_git_blame_porcelain(
                    blame_text, set(range(start_line, end_line + 1))
                ))

        missing = sorted(selected.difference(parsed))
        if missing:
            raise RuntimeError(
                "git blame returned no attribution for {} line(s) in {} at {}: {}".format(
                    len(missing), file_path, newgit, missing[:20]
                )
            )
        for attribution in parsed.values():
            attribution["repository"] = repository_name
        result[file_path] = parsed
    return result


def _parse_lcov_function_payload(payload, tag):
    """Parse common FN/FNL/FNA complete-range and alias forms."""
    parts = [part.strip() for part in str(payload or "").split(",")]
    numeric = []
    for index, part in enumerate(parts):
        try:
            numeric.append((index, int(part)))
        except (TypeError, ValueError):
            continue

    if len(numeric) >= 2:
        # Complete extensions normally use start,end,name.  Accept the
        # equivalent start,name,end form as well because toolchains differ.
        start_index, start_line = numeric[0]
        if numeric[-1][0] == len(parts) - 1 and start_index != numeric[-1][0]:
            end_index, end_line = numeric[-1]
            name_parts = parts[:start_index] + parts[start_index + 1:end_index]
        else:
            end_index, end_line = numeric[1]
            if end_index == start_index + 1:
                name_parts = parts[end_index + 1:]
            else:
                name_parts = parts[start_index + 1:end_index]
        return {
            "start_line": start_line,
            "end_line": end_line,
            "name": ",".join(name_parts).strip(),
        }

    if len(numeric) == 1:
        line_index, line_number = numeric[0]
        name = ",".join(parts[:line_index] + parts[line_index + 1:]).strip()
        if tag == "FN":
            return {"start_line": line_number, "end_line": None, "name": name}
        # FNL/FNA are treated as end-line aliases when the start is supplied
        # by a matching FN record.  They may also be emitted as a complete
        # range by the two-number branch above.
        return {"start_line": None, "end_line": line_number, "name": name}

    return None


def _finalize_lcov_function_state(state):
    direct_ranges = list(state.get("ranges") or [])
    starts = list(state.get("starts") or [])
    ends = list(state.get("ends") or [])
    unmatched = False

    remaining_ends = list(ends)
    for start in starts:
        matching_index = None
        for index, end in enumerate(remaining_ends):
            if end.get("name", "") == start.get("name", ""):
                matching_index = index
                break
        if matching_index is None:
            unmatched = True
            continue
        end = remaining_ends.pop(matching_index)
        direct_ranges.append({
            "start_line": start["start_line"],
            "end_line": end["end_line"],
            "name": start.get("name", "") or end.get("name", ""),
        })

    if remaining_ends:
        unmatched = True

    normalized = normalize_lcov_function_ranges(direct_ranges)
    if unmatched or (starts and not normalized):
        return [], False
    if state.get("has_function_records") and not normalized:
        return [], False
    return normalized, bool(normalized)


def _parse_lcov_info_data_internal(info_file):
    coverage_data = {}
    function_ranges = {}
    function_states = {}
    current_file = None

    def ensure_state(path):
        return function_states.setdefault(path, {
            "has_function_records": False,
            "ranges": [],
            "starts": [],
            "ends": [],
        })

    def finalize_file(path):
        if not path or path not in function_states:
            return
        ranges, trusted = _finalize_lcov_function_state(function_states[path])
        if trusted:
            function_ranges[path] = ranges
        else:
            function_ranges.pop(path, None)

    with open(info_file, "r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if line.startswith("SF:"):
                if current_file:
                    finalize_file(current_file)
                normalized = normalize_path(line[3:])
                if is_valid_source_file(normalized):
                    current_file = normalized
                    coverage_data.setdefault(current_file, {})
                    ensure_state(current_file)
                else:
                    current_file = None
            elif line.startswith("DA:") and current_file:
                parts = line[3:].split(",", 2)
                try:
                    line_number = int(parts[0])
                    execution_count = int(parts[1])
                except (IndexError, ValueError):
                    continue
                coverage_data[current_file][line_number] = execution_count
            elif current_file and (line.startswith("FN:") or line.startswith("FNL:") or line.startswith("FNA:")):
                tag, payload = line.split(":", 1)
                parsed = _parse_lcov_function_payload(payload, tag)
                if not parsed:
                    continue
                state = ensure_state(current_file)
                state["has_function_records"] = True
                if parsed.get("start_line") is not None and parsed.get("end_line") is not None:
                    state["ranges"].append(parsed)
                elif parsed.get("start_line") is not None:
                    state["starts"].append(parsed)
                elif parsed.get("end_line") is not None:
                    state["ends"].append(parsed)
            elif line == "end_of_record":
                finalize_file(current_file)
                current_file = None

    if current_file:
        finalize_file(current_file)
    return coverage_data, function_ranges, function_states


def normalize_lcov_function_ranges(ranges, total_lines=None):
    """Normalize and validate complete LCOV function ranges.

    Nested ranges are compiler aliases and collapse to the outer physical
    function.  Truly crossing ranges are unsafe and return an empty list so
    callers can use the source parser fallback.
    """
    normalized = []
    for raw in ranges or []:
        if isinstance(raw, dict):
            start = raw.get("start_line")
            end = raw.get("end_line")
            name = raw.get("name")
        elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
            start, end = raw[0], raw[1]
            name = raw[2] if len(raw) > 2 else ""
        else:
            start = getattr(raw, "start_line", None)
            end = getattr(raw, "end_line", None)
            name = getattr(raw, "name", "")
        try:
            start = int(start)
            end = int(end)
        except (TypeError, ValueError):
            return []
        if start < 1 or end < start or (total_lines is not None and end > int(total_lines)):
            return []
        normalized.append({
            "start_line": start,
            "end_line": end,
            "name": str(name or "").strip(),
        })

    if not normalized:
        return []

    # Same physical range with different aliases is represented once.
    by_geometry = {}
    for item in normalized:
        key = (item["start_line"], item["end_line"])
        previous = by_geometry.get(key)
        if previous is None or (not previous.get("name") and item.get("name")):
            by_geometry[key] = item

    ordered = sorted(
        by_geometry.values(),
        key=lambda item: (item["start_line"], -item["end_line"]),
    )
    outermost = []
    for item in ordered:
        contained = False
        for existing in outermost:
            if (existing["start_line"] <= item["start_line"] and
                    item["end_line"] <= existing["end_line"]):
                contained = True
                break
            if (item["start_line"] < existing["start_line"] <= item["end_line"] < existing["end_line"] or
                    existing["start_line"] < item["start_line"] <= existing["end_line"] < item["end_line"]):
                return []
        if not contained:
            outermost.append(item)
    return outermost


def parse_lcov_info_data(info_file):
    """Parse LCOV coverage and trusted complete function ranges."""
    coverage_data, function_ranges, _ = _parse_lcov_info_data_internal(info_file)
    return coverage_data, function_ranges


def parse_lcov_info(info_file):
    """Parse one LCOV .info file into ``{source_file: {line: execution_count}}``."""
    coverage_data, _ = parse_lcov_info_data(info_file)
    return coverage_data


def find_info_files(info_path):
    """Return one .info file, or all .info files immediately below a directory."""
    if os.path.isfile(info_path):
        return [info_path]
    if not os.path.isdir(info_path):
        raise ValueError(".info 文件或目录不存在: {}".format(info_path))
    files = [
        os.path.join(info_path, filename)
        for filename in sorted(os.listdir(info_path))
        if filename.endswith(".info") and os.path.isfile(os.path.join(info_path, filename))
    ]
    if not files:
        raise ValueError("{} 下没有找到 .info 文件".format(info_path))
    return files


def load_lcov_info(info_path):
    """Load and merge one or more .info files in Python (no lcov binary is required)."""
    merged = {}
    info_files = find_info_files(info_path)
    for info_file in info_files:
        for file_path, lines in parse_lcov_info(info_file).items():
            target_lines = merged.setdefault(file_path, {})
            for line_number, execution_count in lines.items():
                target_lines[line_number] = target_lines.get(line_number, 0) + execution_count
    return merged, info_files


def load_lcov_info_with_functions(info_path):
    """Load coverage and merge only files with trusted complete ranges."""
    merged, function_ranges = {}, {}
    invalid_function_files = set()
    info_files = find_info_files(info_path)
    for info_file in info_files:
        coverage_data, ranges, states = _parse_lcov_info_data_internal(info_file)
        for file_path, lines in coverage_data.items():
            target_lines = merged.setdefault(file_path, {})
            for line_number, execution_count in lines.items():
                target_lines[line_number] = target_lines.get(line_number, 0) + execution_count
        for file_path, state in states.items():
            if not state.get("has_function_records"):
                continue
            if file_path not in ranges:
                invalid_function_files.add(file_path)
            else:
                function_ranges.setdefault(file_path, []).extend(ranges[file_path])

    for file_path in list(function_ranges):
        if file_path in invalid_function_files:
            function_ranges.pop(file_path, None)
            continue
        normalized = normalize_lcov_function_ranges(function_ranges[file_path])
        if normalized:
            function_ranges[file_path] = normalized
        else:
            function_ranges.pop(file_path, None)
    return merged, function_ranges, info_files


def merge_info_files(info_path):
    """Compatibility shim for callers of the old script.

    Multi-file analysis is now performed directly by :func:`load_lcov_info`; callers
    should pass the original path to that function instead of relying on an lcov CLI
    merge side effect.
    """
    if os.path.isfile(info_path):
        return info_path
    find_info_files(info_path)
    return info_path


def resolve_coverage_file(filename, coverage_data, repo_path):
    """Resolve a Git-relative path to an LCOV SF entry, avoiding ambiguous suffixes with indexed lookups."""
    filename = normalize_path(filename)
    repo_relative = normalize_path(os.path.join(repo_path, filename))
    candidates = (filename, repo_relative)
    for candidate in candidates:
        if candidate in coverage_data:
            return candidate

    # Build once per immutable coverage key set.  Resolution itself is O(1)
    # for exact/normalized paths and O(path depth) for suffixes.
    cache_key = (id(coverage_data), tuple(coverage_data.keys()))
    index = _PATH_INDEX_CACHE.get(cache_key)
    if index is None:
        index = IncrementalService({"repo": list(coverage_data.keys())}).path_index
        _PATH_INDEX_CACHE[cache_key] = index
    resolved, match_type = index.resolve_path("repo", filename)
    if resolved:
        return resolved
    # Absolute/repository-prefixed candidates are handled as normalized exact
    # lookups before the safe suffix resolver.
    resolved, _ = index.resolve_path("repo", repo_relative)
    return resolved


def load_repositories_config(config_path):
    """Load and validate the independent Git repositories used by multi-repo mode."""
    config_path = os.path.abspath(config_path)
    with open(config_path, "r", encoding="utf-8") as config_file:
        raw_config = json.load(config_file)

    repositories = raw_config.get("repositories") if isinstance(raw_config, dict) else raw_config
    if not isinstance(repositories, list) or not repositories:
        raise ValueError("仓库配置必须包含非空 repositories 数组: {}".format(config_path))

    config_dir = os.path.dirname(config_path)
    result = []
    names = set()
    paths = set()
    for index, item in enumerate(repositories, start=1):
        if not isinstance(item, dict):
            raise ValueError("repositories 第 {} 项必须是对象".format(index))
        name = str(item.get("name") or "").strip()
        repo_path = str(item.get("path") or item.get("repo") or "").strip()
        oldgit = str(item.get("oldgit") or "").strip()
        newgit = str(item.get("newgit") or "").strip()
        if not name or not repo_path or not oldgit or not newgit:
            raise ValueError(
                "repositories 第 {} 项需要 name、path、oldgit、newgit".format(index)
            )
        if not os.path.isabs(repo_path):
            repo_path = os.path.join(config_dir, repo_path)
        repo_path = os.path.abspath(repo_path)
        if name in names:
            raise ValueError("仓库配置中 name 重复: {}".format(name))
        if repo_path in paths:
            raise ValueError("仓库配置中 path 重复: {}".format(repo_path))
        if not os.path.isdir(repo_path):
            raise ValueError("Git 仓库目录不存在: {}".format(repo_path))
        names.add(name)
        paths.add(repo_path)
        result.append({
            "name": name,
            "path": repo_path,
            "oldgit": oldgit,
            "newgit": newgit,
        })
    return result


def build_summary(counters):
    """Build the common summary fields from line-status counters."""
    covered = counters[STATUS_COVERED]
    uncovered = counters[STATUS_UNCOVERED]
    ignored = counters[STATUS_IGNORED]
    missing = counters[STATUS_MISSING]
    coverable_total = covered + uncovered
    changed_total = covered + uncovered + ignored + missing
    return {
        "changed_lines": changed_total,
        "covered": covered,
        "uncovered": uncovered,
        "ignored": ignored,
        "missing": missing,
        "unanalyzed": uncovered,
        "coverable_total": coverable_total,
        "coverage_rate": covered * 100.0 / coverable_total if coverable_total else None,
    }


def _build_legacy_developer_tasks(details, developer_file_changes):
    """Join Git authors with the coverage summary for each changed file.

    The coverage result is calculated from the final diff, while Git history is
    calculated per commit.  Therefore a jointly modified file is deliberately
    listed for every author who touched it; this makes the hand-off visible
    instead of arbitrarily assigning the file to the last committer.
    """
    metrics_by_file = {}
    for item in details:
        key = (item.get("repository", ""), normalize_path(item.get("file_path", "")))
        metrics = metrics_by_file.setdefault(key, {
            "repository": item.get("repository", ""),
            "file_path": key[1],
            "review_file_path": item.get("review_file_path") or item.get("coverage_file") or key[1],
            "changed": 0,
            "covered": 0,
            "uncovered": 0,
            "ignored": 0,
            "missing": 0,
        })
        metrics["changed"] += 1
        status = item.get("status")
        if status == STATUS_COVERED:
            metrics["covered"] += 1
        elif status == STATUS_UNCOVERED:
            metrics["uncovered"] += 1
        elif status == STATUS_IGNORED:
            metrics["ignored"] += 1
        elif status == STATUS_MISSING:
            metrics["missing"] += 1

    developers = {}
    for change in developer_file_changes or []:
        repository = change.get("repository", "")
        file_path = normalize_path(change.get("file_path", ""))
        if not file_path:
            continue
        author_name = change.get("author_name") or "Unknown"
        author_email = change.get("author_email") or ""
        # Email is the stable identity.  Name fallback keeps older repositories
        # with missing author email address usable as well.
        identity = author_email.strip().lower() or author_name.strip().lower()
        if not identity:
            identity = "unknown"
        developer = developers.setdefault(identity, {
            "name": author_name,
            "email": author_email,
            "commits": {},
            "files": {},
        })

        commit_id = change.get("commit") or ""
        if commit_id:
            developer["commits"]["{}:{}".format(repository, commit_id)] = {
                "repository": repository,
                "commit": commit_id,
                "committed_at": change.get("committed_at") or "",
                "subject": change.get("subject") or "",
            }

        file_key = (repository, file_path)
        file_task = developer["files"].setdefault(file_key, {
            "repository": repository,
            "file_path": file_path,
            "change_types": set(),
            "commits": {},
        })
        change_type = change.get("change_type") or ""
        if change_type:
            file_task["change_types"].add(change_type)
        if commit_id:
            file_task["commits"][commit_id] = {
                "commit": commit_id,
                "committed_at": change.get("committed_at") or "",
                "subject": change.get("subject") or "",
            }

    result_developers = []
    for developer in developers.values():
        tasks = []
        for file_task in developer["files"].values():
            metrics = metrics_by_file.get(
                (file_task["repository"], file_task["file_path"]), {}
            )
            task = {
                "repository": file_task["repository"],
                "file_path": file_task["file_path"],
                "review_file_path": metrics.get("review_file_path", file_task["file_path"]),
                "change_types": sorted(file_task["change_types"]),
                "commits": sorted(
                    file_task["commits"].values(),
                    key=lambda item: (item["committed_at"], item["commit"]),
                    reverse=True,
                ),
                "changed": metrics.get("changed", 0),
                "covered": metrics.get("covered", 0),
                "uncovered": metrics.get("uncovered", 0),
                "ignored": metrics.get("ignored", 0),
                "missing": metrics.get("missing", 0),
            }
            tasks.append(task)

        tasks.sort(key=lambda item: (
            -item["uncovered"], -item["changed"], item["repository"], item["file_path"]
        ))
        commits = sorted(
            developer["commits"].values(),
            key=lambda item: (item["committed_at"], item["commit"]),
            reverse=True,
        )
        result_developers.append({
            "name": developer["name"],
            "email": developer["email"],
            "commit_total": len(commits),
            "changed_file_total": len(tasks),
            "review_file_total": sum(1 for item in tasks if item["uncovered"]),
            "review_uncovered_total": sum(item["uncovered"] for item in tasks),
            "commits": commits,
            "files": tasks,
        })

    result_developers.sort(key=lambda item: (
        -item["review_uncovered_total"], -item["review_file_total"],
        item["name"].lower(), item["email"].lower(),
    ))
    return {"developers": result_developers}


def _developer_identity(name, email):
    email = str(email or "").strip()
    name = str(name or "").strip()
    return email.lower() or name.lower() or "unknown"


def _new_precise_developer_task():
    return {
        "repository": "",
        "file_path": "",
        "review_file_path": "",
        "change_types": set(),
        "commits": {},
        "owned_line_numbers": set(),
        "covered_line_numbers": set(),
        "uncovered_line_numbers": set(),
        "ignored_line_numbers": set(),
        "missing_line_numbers": set(),
    }


def build_developer_tasks(details, developer_file_changes):
    """Aggregate developer tasks from final-line attribution when available.

    New schema-v3 details carry one author per added line.  That path counts
    only the lines owned by the developer.  The legacy file-level aggregation
    remains available for callers that pass pre-v3 details without attribution.
    """
    has_attribution = any(
        item.get("author_name") or item.get("author_email") or item.get("commit")
        for item in (details or [])
    )
    if not has_attribution:
        return _build_legacy_developer_tasks(details, developer_file_changes)

    developers = {}

    def ensure_developer(name, email):
        identity = _developer_identity(name, email)
        developer = developers.setdefault(identity, {
            "name": str(name or "Unknown"),
            "email": str(email or ""),
            "commits": {},
            "files": {},
        })
        if not developer["email"] and email:
            developer["email"] = str(email)
        if developer["name"] == "Unknown" and name:
            developer["name"] = str(name)
        return identity, developer

    def ensure_file_task(developer, repository, file_path, review_file_path=""):
        key = (repository, normalize_path(file_path))
        task = developer["files"].setdefault(key, _new_precise_developer_task())
        task["repository"] = repository
        task["file_path"] = normalize_path(file_path)
        task["review_file_path"] = review_file_path or task["review_file_path"] or task["file_path"]
        return task

    for item in details or []:
        name = item.get("author_name") or "Unknown"
        email = item.get("author_email") or ""
        identity, developer = ensure_developer(name, email)
        repository = item.get("repository", "") or ""
        file_path = normalize_path(item.get("file_path", ""))
        if not file_path:
            continue
        task = ensure_file_task(
            developer, repository, file_path,
            item.get("review_file_path") or item.get("coverage_file") or file_path,
        )
        line_number = int(item.get("line_number", 0) or 0)
        if line_number > 0:
            task["owned_line_numbers"].add(line_number)
            status = item.get("status")
            if status == STATUS_COVERED:
                task["covered_line_numbers"].add(line_number)
            elif status == STATUS_UNCOVERED:
                task["uncovered_line_numbers"].add(line_number)
            elif status == STATUS_IGNORED:
                task["ignored_line_numbers"].add(line_number)
            elif status == STATUS_MISSING:
                task["missing_line_numbers"].add(line_number)

        commit_id = item.get("commit") or ""
        if commit_id:
            commit_data = {
                "repository": repository,
                "commit": commit_id,
                "committed_at": item.get("committed_at") or "",
                "subject": item.get("subject") or "",
            }
            developer["commits"]["{}:{}".format(repository, commit_id)] = commit_data
            task["commits"][commit_id] = dict(commit_data)

    # Add commit/change metadata from git log without assigning those commits'
    # unrelated lines to the developer.  This preserves traceability while the
    # line counters remain strictly blame-owned.
    for change in developer_file_changes or []:
        identity, developer = ensure_developer(
            change.get("author_name") or "Unknown",
            change.get("author_email") or "",
        )
        repository = change.get("repository", "") or ""
        file_path = normalize_path(change.get("file_path", ""))
        if not file_path:
            continue
        task = developer["files"].get((repository, file_path))
        if task is None and not has_attribution:
            task = ensure_file_task(developer, repository, file_path)
        if task is None:
            continue
        change_type = change.get("change_type") or ""
        if change_type:
            task["change_types"].add(change_type)
        commit_id = change.get("commit") or ""
        if commit_id:
            commit_data = {
                "repository": repository,
                "commit": commit_id,
                "committed_at": change.get("committed_at") or "",
                "subject": change.get("subject") or "",
            }
            developer["commits"]["{}:{}".format(repository, commit_id)] = commit_data
            task["commits"][commit_id] = {
                key: value for key, value in commit_data.items() if key != "repository"
            }

    result_developers = []
    for developer in developers.values():
        tasks = []
        for task_data in developer["files"].values():
            owned = sorted(task_data["owned_line_numbers"])
            covered = sorted(task_data["covered_line_numbers"])
            uncovered = sorted(task_data["uncovered_line_numbers"])
            ignored = sorted(task_data["ignored_line_numbers"])
            missing = sorted(task_data["missing_line_numbers"])
            tasks.append({
                "repository": task_data["repository"],
                "file_path": task_data["file_path"],
                "review_file_path": task_data["review_file_path"],
                "change_types": sorted(task_data["change_types"]),
                "commits": sorted(
                    task_data["commits"].values(),
                    key=lambda item: (item.get("committed_at", ""), item.get("commit", "")),
                    reverse=True,
                ),
                "owned_added_lines": len(owned),
                "owned_line_numbers": owned,
                "changed": len(owned),
                "covered": len(covered),
                "uncovered": len(uncovered),
                "uncovered_need_fill": len(uncovered),
                "uncovered_line_numbers": uncovered,
                "uncovered_need_fill_line_numbers": uncovered,
                "ignored": len(ignored),
                "missing": len(missing),
                "ignored_line_numbers": ignored,
                "missing_line_numbers": missing,
            })
        tasks.sort(key=lambda item: (
            -item["uncovered"], -item["owned_added_lines"],
            item["repository"], item["file_path"],
        ))
        commits = sorted(
            developer["commits"].values(),
            key=lambda item: (item.get("committed_at", ""), item.get("commit", "")),
            reverse=True,
        )
        result_developers.append({
            "name": developer["name"],
            "email": developer["email"],
            "commit_total": len(commits),
            "changed_file_total": len(tasks),
            "owned_added_lines": sum(item["owned_added_lines"] for item in tasks),
            "owned_line_numbers": sorted({
                line for item in tasks for line in item["owned_line_numbers"]
            }),
            "review_file_total": sum(1 for item in tasks if item["uncovered"]),
            "review_uncovered_total": sum(item["uncovered"] for item in tasks),
            "uncovered_line_numbers": sorted({
                line for item in tasks for line in item["uncovered_line_numbers"]
            }),
            "commits": commits,
            "files": tasks,
        })

    result_developers.sort(key=lambda item: (
        -item["review_uncovered_total"], -item["owned_added_lines"],
        item["name"].lower(), item["email"].lower(),
    ))
    return {"developers": result_developers}


def _lookup_file_mapping(mapping, file_path, resolver=None):
    """Resolve a path-keyed metadata map with the canonical safe index."""
    if not mapping:
        return None
    resolver = resolver or IncrementalService({"default": list(mapping.keys())})
    value, match_type = resolver.resolve_mapping_value(file_path, mapping)
    if match_type not in ("exact", "normalized", "unique_suffix"):
        return None
    return value


def calculate_repository_coverage(repo_path, oldgit, newgit, coverage_data, info_files,
                                  diff_text=None, repository_name="", developer_file_changes=None,
                                  line_authors_by_file=None, function_ranges_data=None):
    """Calculate one repository against already-loaded LCOV data."""
    repo_path = os.path.abspath(repo_path)
    if diff_text is None:
        diff_text = run_git_diff(repo_path, oldgit, newgit)
    if developer_file_changes is None:
        developer_file_changes = run_git_developer_file_changes(
            repo_path, oldgit, newgit, repository_name
        )
    changes = parse_diff_text(diff_text)

    if line_authors_by_file is None:
        # A real Git repository must always use blame pinned to newgit.  The
        # explicit empty developer-change input is retained for legacy/import
        # callers that classify an already supplied diff without a .git dir.
        has_git_metadata = os.path.exists(os.path.join(repo_path, ".git"))
        if changes and has_git_metadata:
            line_authors_by_file = run_git_line_authors(
                repo_path, newgit, changes, repository_name
            )
        else:
            line_authors_by_file = {}

    line_author_resolver = (
        IncrementalService({"default": list(line_authors_by_file.keys())})
        if line_authors_by_file else None
    )
    function_range_resolver = (
        IncrementalService({"default": list(function_ranges_data.keys())})
        if function_ranges_data else None
    )

    details = []
    counters = defaultdict(int)
    uncovered_lines_by_file = defaultdict(list)
    review_lines_by_file = defaultdict(list)
    reviewers_by_file = defaultdict(dict)
    trusted_function_ranges_by_file = {}
    for filename in sorted(changes):
        coverage_file = resolve_coverage_file(filename, coverage_data, repo_path)
        line_data = coverage_data.get(coverage_file, {}) if coverage_file else None
        author_map = _lookup_file_mapping(
            line_authors_by_file, filename, line_author_resolver
        ) or {}
        function_ranges = None
        if coverage_file and function_ranges_data:
            function_ranges = _lookup_file_mapping(
                function_ranges_data, coverage_file, function_range_resolver
            )
            if function_ranges is None:
                function_ranges = _lookup_file_mapping(
                    function_ranges_data, filename, function_range_resolver
                )
            normalized_ranges = normalize_lcov_function_ranges(function_ranges)
            if normalized_ranges:
                trusted_function_ranges_by_file[coverage_file] = normalized_ranges
        for line_number in sorted(set(changes[filename])):
            execution_count = None
            if line_data is None:
                status = STATUS_MISSING
            elif line_number not in line_data:
                status = STATUS_IGNORED
            else:
                execution_count = line_data[line_number]
                status = STATUS_COVERED if execution_count > 0 else STATUS_UNCOVERED
            counters[status] += 1
            attribution = author_map.get(line_number) or author_map.get(str(line_number)) or {}
            author_name = str(attribution.get("author_name", "") or "").strip()
            author_email = str(attribution.get("author_email", "") or "").strip()
            suggested_reviewer = author_name
            if status == STATUS_UNCOVERED:
                uncovered_lines_by_file[filename].append(line_number)
                review_file_path = coverage_file or normalize_path(os.path.join(repo_path, filename))
                review_lines_by_file[review_file_path].append(line_number)
                if suggested_reviewer:
                    reviewers_by_file[review_file_path][str(line_number)] = suggested_reviewer
            detail = {
                "repository": repository_name,
                "file_path": filename,
                "coverage_file": coverage_file or "",
                "review_file_path": coverage_file or normalize_path(os.path.join(repo_path, filename)),
                "line_number": line_number,
                "execution_count": execution_count,
                "status": status,
                "author_name": author_name,
                "author_email": author_email,
                "reviewer": suggested_reviewer,
                "commit": attribution.get("commit", "") or "",
                "committed_at": attribution.get("committed_at", "") or "",
                "subject": attribution.get("subject", "") or "",
            }
            details.append(detail)

    return {
        "schema_version": 3,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "repo_path": repo_path,
        "oldgit": oldgit,
        "newgit": newgit,
        "info_files": [os.path.abspath(path) for path in info_files],
        "summary": build_summary(counters),
        "details": details,
        "uncovered_lines_by_file": {
            filename: sorted(lines) for filename, lines in sorted(uncovered_lines_by_file.items())
        },
        "review_lines_by_file": {
            filename: sorted(lines) for filename, lines in sorted(review_lines_by_file.items())
        },
        "reviewers_by_file": {
            filename: {str(line): reviewer for line, reviewer in sorted(reviewers.items(), key=lambda item: int(item[0]))}
            for filename, reviewers in sorted(reviewers_by_file.items())
        },
        "function_ranges_by_file": {
            filename: ranges for filename, ranges in sorted(trusted_function_ranges_by_file.items())
        },
        "developer_file_changes": developer_file_changes,
        "developer_tasks": build_developer_tasks(details, developer_file_changes),
    }


def calculate_incremental_coverage(repo_path, oldgit, newgit, info_path, diff_text=None,
                                   developer_file_changes=None, line_authors_by_file=None):
    """Calculate coverage of Git-added lines in one repository."""
    coverage_data, function_ranges_data, info_files = load_lcov_info_with_functions(info_path)
    return calculate_repository_coverage(
        repo_path, oldgit, newgit, coverage_data, info_files, diff_text,
        developer_file_changes=developer_file_changes,
        line_authors_by_file=line_authors_by_file,
        function_ranges_data=function_ranges_data,
    )


def calculate_multi_repo_incremental_coverage(repositories, info_path, line_authors_by_repo=None):
    """Calculate one combined incremental report for several independent Git repos.

    A multi-repo LCOV file must use absolute ``SF:`` paths. Without that identity,
    identical relative source paths from different repositories cannot be matched
    safely to their respective Git diff.
    """
    if not repositories:
        raise ValueError("至少需要一个 Git 仓库")
    coverage_data, function_ranges_data, info_files = load_lcov_info_with_functions(info_path)
    details = []
    counters = defaultdict(int)
    raw_uncovered_lines = {}
    review_lines_by_file = defaultdict(list)
    reviewers_by_file = defaultdict(dict)
    function_ranges_by_file = {}
    repository_summaries = []
    developer_file_changes = []

    for repository in repositories:
        name = repository["name"]
        repo_path = repository["path"]
        result = calculate_repository_coverage(
            repo_path,
            repository["oldgit"],
            repository["newgit"],
            coverage_data,
            info_files,
            repository_name=name,
            line_authors_by_file=(line_authors_by_repo or {}).get(name),
            function_ranges_data=function_ranges_data,
        )
        for item in result["details"]:
            coverage_file = item.get("coverage_file")
            if coverage_file and not os.path.isabs(coverage_file):
                raise ValueError(
                    "多仓库模式要求 .info 的 SF 路径为绝对路径；发现: {}。"
                    "请重新生成 LCOV .info 后重试。".format(coverage_file)
                )
            counters[item["status"]] += 1
        details.extend(result["details"])
        developer_file_changes.extend(result.get("developer_file_changes") or [])
        for file_path, lines in result["uncovered_lines_by_file"].items():
            raw_uncovered_lines["{}:{}".format(name, file_path)] = lines
        for file_path, lines in result["review_lines_by_file"].items():
            review_lines_by_file[file_path].extend(lines)
        for file_path, reviewers in (result.get("reviewers_by_file") or {}).items():
            reviewers_by_file[file_path].update(reviewers)
        function_ranges_by_file.update(result.get("function_ranges_by_file") or {})
        repository_summaries.append({
            "name": name,
            "path": repo_path,
            "oldgit": repository["oldgit"],
            "newgit": repository["newgit"],
            "summary": result["summary"],
        })

    return {
        "schema_version": 3,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "repo_path": "",
        "oldgit": "multiple",
        "newgit": "multiple",
        "repositories": repository_summaries,
        "info_files": [os.path.abspath(path) for path in info_files],
        "summary": build_summary(counters),
        "details": details,
        "uncovered_lines_by_file": raw_uncovered_lines,
        "review_lines_by_file": {
            filename: sorted(set(lines)) for filename, lines in sorted(review_lines_by_file.items())
        },
        "reviewers_by_file": {
            filename: {
                str(line): reviewer
                for line, reviewer in sorted(
                    reviewers.items(), key=lambda item: int(item[0])
                )
            }
            for filename, reviewers in sorted(reviewers_by_file.items())
        },
        "function_ranges_by_file": {
            filename: ranges for filename, ranges in sorted(function_ranges_by_file.items())
        },
        "developer_file_changes": developer_file_changes,
        "developer_tasks": build_developer_tasks(details, developer_file_changes),
    }


def calculate_multi_repo_incremental_coverage_from_config(config_path, info_path):
    """Load a repository config file and calculate its combined report."""
    return calculate_multi_repo_incremental_coverage(
        load_repositories_config(config_path), info_path
    )


def check_coverage(info_file, changes, out_excel, repo_path):
    """Backward-compatible entry point used by legacy callers."""
    del repo_path
    coverage_data, _ = load_lcov_info(info_file)
    # The legacy function receives explicit changed line numbers rather than the
    # original hunk layout, so classify those lines directly.
    details = []
    counters = defaultdict(int)
    for filename, line_numbers in changes.items():
        coverage_file = resolve_coverage_file(filename, coverage_data, "")
        line_data = coverage_data.get(coverage_file, {}) if coverage_file else None
        for line_number in sorted(set(line_numbers)):
            execution_count = line_data.get(line_number) if line_data is not None else None
            status = (STATUS_MISSING if line_data is None else STATUS_IGNORED if line_number not in line_data
                      else STATUS_COVERED if execution_count > 0 else STATUS_UNCOVERED)
            counters[status] += 1
            details.append({"file_path": filename, "coverage_file": coverage_file or "", "line_number": line_number,
                            "execution_count": execution_count, "status": status})
    result = {"details": details, "summary": {
        "changed_lines": len(details), "covered": counters[STATUS_COVERED], "uncovered": counters[STATUS_UNCOVERED],
        "ignored": counters[STATUS_IGNORED], "missing": counters[STATUS_MISSING],
        "coverable_total": counters[STATUS_COVERED] + counters[STATUS_UNCOVERED],
    }}
    total = result["summary"]["coverable_total"]
    result["summary"]["coverage_rate"] = result["summary"]["covered"] * 100.0 / total if total else None
    write_result_excel(result, out_excel)
    return result


def write_result_json(result, output_path):
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)


def _xlsx_column_name(index):
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _xlsx_cell(row_index, column_index, value, header=False):
    ref = "{}{}".format(_xlsx_column_name(column_index), row_index)
    style = ' s="1"' if header else ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return '<c r="{}"{}><v>{}</v></c>'.format(ref, style, value)
    text = html_lib.escape("" if value is None else str(value), quote=True)
    return '<c r="{}" t="inlineStr"{}><is><t>{}</t></is></c>'.format(ref, style, text)


def _xlsx_sheet(rows):
    xml = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>']
    xml.append('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>')
    for row_index, row in enumerate(rows, start=1):
        xml.append('<row r="{}">'.format(row_index))
        for column_index, value in enumerate(row, start=1):
            xml.append(_xlsx_cell(row_index, column_index, value, row_index == 1))
        xml.append("</row>")
    xml.append("</sheetData></worksheet>")
    return "".join(xml)


def _build_simple_xlsx(sheet_defs):
    """Create a standards-compliant XLSX using only the Python standard library."""
    content_types = [
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
    ]
    workbook_sheets, rels = [], []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("_rels/.rels", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>''')
        archive.writestr("xl/styles.xml", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs></styleSheet>''')
        for index, (name, rows) in enumerate(sheet_defs, start=1):
            archive.writestr("xl/worksheets/sheet{}.xml".format(index), _xlsx_sheet(rows))
            workbook_sheets.append('<sheet name="{}" sheetId="{}" r:id="rId{}"/>'.format(
                html_lib.escape(name[:31], quote=True), index, index
            ))
            rels.append('<Relationship Id="rId{}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{}.xml"/>'.format(index, index))
            content_types.append('<Override PartName="/xl/worksheets/sheet{}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'.format(index))
        archive.writestr("xl/workbook.xml", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{}</sheets></workbook>'''.format("".join(workbook_sheets)))
        rels.append('<Relationship Id="rId{}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'.format(len(sheet_defs) + 1))
        archive.writestr("xl/_rels/workbook.xml.rels", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{}</Relationships>'''.format("".join(rels)))
        archive.writestr("[Content_Types].xml", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">{}</Types>'''.format("".join(content_types)))
    return buffer.getvalue()


def write_result_excel(result, output_path):
    """Write incremental line details plus developer-facing task sheets."""
    details_rows = [[
        "Repository", "File", "Coverage File", "Line", "Execution Count", "Coverage",
        "Developer", "Email", "Reviewer", "Blame Commit", "Commit Subject",
    ]]
    for item in result["details"]:
        details_rows.append([
            item.get("repository", ""), item["file_path"], item.get("coverage_file", ""),
            item["line_number"], item.get("execution_count"), item["status"],
            item.get("author_name", ""), item.get("author_email", ""),
            item.get("reviewer", ""), item.get("commit", ""), item.get("subject", ""),
        ])

    summary = result["summary"]
    summary_rows = [["类别", "数量", "比例"]]
    total = summary["coverable_total"]
    for label, key in ((STATUS_COVERED, "covered"), (STATUS_UNCOVERED, "uncovered")):
        value = summary[key]
        summary_rows.append([label, value, "{:.2f}%".format(value * 100.0 / total) if total else "N/A"])
    changed_total = summary["changed_lines"]
    for label, key in ((STATUS_IGNORED, "ignored"), (STATUS_MISSING, "missing")):
        value = summary[key]
        summary_rows.append([label, value, "{:.2f}%".format(value * 100.0 / changed_total) if changed_total else "N/A"])
    unanalyzed_val = summary.get("unanalyzed", summary.get("uncovered", 0))
    summary_rows.append(["待分析", unanalyzed_val, "{:.2f}%".format(unanalyzed_val * 100.0 / changed_total) if changed_total else "N/A"])
    summary_rows.append([])
    rate = summary["coverage_rate"]
    summary_rows.append(["覆盖率(有效增量行)", "{}/{}".format(summary["covered"], total),
                         "{:.2f}%".format(rate) if rate is not None else "N/A"])
    summary_rows.append(["总增量行", changed_total, "100%" if changed_total else "N/A"])
    sheet_defs = [("Details", details_rows), ("Summary", summary_rows)]
    repositories = result.get("repositories") or []
    if repositories:
        repository_rows = [[
            "Repository", "Path", "Old Commit", "New Commit", "Changed Lines",
            "Covered", "Uncovered", "Coverage Rate",
        ]]
        for repository in repositories:
            repository_summary = repository["summary"]
            rate = repository_summary["coverage_rate"]
            repository_rows.append([
                repository["name"], repository["path"], repository["oldgit"], repository["newgit"],
                repository_summary["changed_lines"], repository_summary["covered"],
                repository_summary["uncovered"], "{:.2f}%".format(rate) if rate is not None else "N/A",
            ])
        sheet_defs.append(("Repositories", repository_rows))

    developer_tasks = result.get("developer_tasks") or {}
    developer_rows = [[
        "Developer", "Email", "Commits", "Changed Files", "Owned Added Lines",
        "Owned Line Numbers", "Files Need Fill", "Uncovered Lines Need Fill",
        "Uncovered Line Numbers",
    ]]
    developer_file_rows = [[
        "Developer", "Email", "Repository", "File", "Change Types", "Commits",
        "Owned Added Lines", "Owned Line Numbers", "Covered", "Uncovered Need Fill",
        "Uncovered Line Numbers", "Ignored", "Coverage Missing", "Commit Subjects",
    ]]
    for developer in developer_tasks.get("developers") or []:
        developer_rows.append([
            developer.get("name", ""), developer.get("email", ""),
            developer.get("commit_total", 0), developer.get("changed_file_total", 0),
            developer.get("owned_added_lines", 0),
            ", ".join(str(line) for line in developer.get("owned_line_numbers", [])),
            developer.get("review_file_total", 0), developer.get("review_uncovered_total", 0),
            ", ".join(str(line) for line in developer.get("uncovered_line_numbers", [])),
        ])
        for file_task in developer.get("files") or []:
            commits = file_task.get("commits") or []
            developer_file_rows.append([
                developer.get("name", ""), developer.get("email", ""),
                file_task.get("repository", ""), file_task.get("file_path", ""),
                ", ".join(file_task.get("change_types") or []),
                ", ".join(item.get("commit", "")[:12] for item in commits),
                file_task.get("owned_added_lines", file_task.get("changed", 0)),
                ", ".join(str(line) for line in file_task.get("owned_line_numbers", [])),
                file_task.get("covered", 0), file_task.get("uncovered_need_fill", file_task.get("uncovered", 0)),
                ", ".join(str(line) for line in file_task.get("uncovered_need_fill_line_numbers", file_task.get("uncovered_line_numbers", []))),
                file_task.get("ignored", 0), file_task.get("missing", 0),
                " | ".join(item.get("subject", "") for item in commits),
            ])
    sheet_defs.extend([
        ("Developer Summary", developer_rows),
        ("Developer Files", developer_file_rows),
    ])
    with open(output_path, "wb") as handle:
        handle.write(_build_simple_xlsx(sheet_defs))


def print_result(result):
    summary = result["summary"]
    print("\n[RESULT] 增量覆盖率统计:")
    print("  已覆盖:       {}".format(summary["covered"]))
    print("  未覆盖:       {}".format(summary["uncovered"]))
    print("  无需覆盖:     {}".format(summary["ignored"]))
    print("  覆盖信息缺失: {}".format(summary["missing"]))
    if summary["coverage_rate"] is None:
        print("  覆盖率(有效增量行): 无有效增量行")
    else:
        print("  覆盖率(有效增量行) = {}/{} = {:.2f}%".format(
            summary["covered"], summary["coverable_total"], summary["coverage_rate"]
        ))
    for repository in result.get("repositories") or []:
        repository_summary = repository["summary"]
        rate = repository_summary["coverage_rate"]
        rate_text = "N/A" if rate is None else "{:.2f}%".format(rate)
        print("  [{}] 新增={}，已覆盖={}，未覆盖={}，覆盖率={}".format(
            repository["name"], repository_summary["changed_lines"],
            repository_summary["covered"], repository_summary["uncovered"], rate_text,
        ))


def main():
    parser = argparse.ArgumentParser(description="Git 增量代码覆盖率检测（基于 LCOV .info 文件）")
    parser.add_argument("--repo", help="单仓库模式：Git 仓库路径")
    parser.add_argument("--oldgit", help="单仓库模式：旧 commit")
    parser.add_argument("--newgit", help="单仓库模式：新 commit")
    parser.add_argument("--repos-config", help="多仓库模式：仓库 JSON 配置文件")
    parser.add_argument("--info", required=True, help=".info 文件或目录")
    parser.add_argument("--excel", default="coverage_result.xlsx", help="结果 Excel 文件")
    parser.add_argument("--json", dest="json_path", help="可选：结果 JSON 文件")
    parser.add_argument("--steps", nargs="+", choices=["diff", "check"], default=["diff", "check"],
                        help="仅单仓库模式：diff=生成 diff.patch，check=计算覆盖率")
    args = parser.parse_args()

    if args.repos_config:
        if args.repo or args.oldgit or args.newgit:
            parser.error("--repos-config 不能与 --repo、--oldgit、--newgit 一起使用")
        if args.steps != ["diff", "check"]:
            parser.error("多仓库模式始终执行 diff + check，不支持 --steps")
        result = calculate_multi_repo_incremental_coverage_from_config(
            args.repos_config, args.info
        )
    else:
        missing = [name for name, value in (
            ("--repo", args.repo), ("--oldgit", args.oldgit), ("--newgit", args.newgit)
        ) if not value]
        if missing:
            parser.error("单仓库模式需要 {}，或改用 --repos-config".format("、".join(missing)))
        diff_file = os.path.join(os.path.abspath(args.repo), "diff.patch")
        diff_text = None
        if "diff" in args.steps:
            diff_text = generate_diff_files(args.repo, args.oldgit, args.newgit, diff_file)
        if "check" not in args.steps:
            return
        if diff_text is None:
            if not os.path.exists(diff_file):
                parser.error("--steps check 需要已有 {}，或同时执行 diff".format(diff_file))
            with open(diff_file, "r", encoding="utf-8", errors="replace") as handle:
                diff_text = handle.read()
        result = calculate_incremental_coverage(args.repo, args.oldgit, args.newgit, args.info, diff_text)

    if result:
        write_result_excel(result, args.excel)
        if args.json_path:
            write_result_json(result, args.json_path)
            print("[INFO] 结果 JSON 已写入 {}".format(args.json_path))
        print("[INFO] 结果已写入 {}".format(args.excel))
        print_result(result)


if __name__ == "__main__":
    main()
