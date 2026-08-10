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
from datetime import datetime


STATUS_COVERED = "已覆盖"
STATUS_UNCOVERED = "未覆盖"
STATUS_IGNORED = "无需覆盖"
STATUS_MISSING = "覆盖信息缺失"


def normalize_path(path):
    """Return a platform-independent normalized path without a leading ``./``."""
    normalized = os.path.normpath(str(path or "")).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def run_git_diff(repo_path, oldgit, newgit):
    """Return the textual diff between two revisions without invoking a shell."""
    proc = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--no-color", oldgit, newgit],
        cwd=repo_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        message = proc.stderr.strip() or "git diff failed"
        raise RuntimeError("git diff {} {} failed: {}".format(oldgit, newgit, message))
    return proc.stdout


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
                current_file = normalize_path(candidate[2:])
                file_changes.setdefault(current_file, [])
            else:
                current_file = normalize_path(candidate)
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


def parse_lcov_info(info_file):
    """Parse one LCOV .info file into ``{source_file: {line: execution_count}}``."""
    coverage_data = {}
    current_file = None
    with open(info_file, "r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if line.startswith("SF:"):
                current_file = normalize_path(line[3:])
                coverage_data.setdefault(current_file, {})
            elif line.startswith("DA:") and current_file:
                parts = line[3:].split(",", 2)
                try:
                    line_number = int(parts[0])
                    execution_count = int(parts[1])
                except (IndexError, ValueError):
                    continue
                coverage_data[current_file][line_number] = execution_count
            elif line == "end_of_record":
                current_file = None
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
    """Resolve a Git-relative path to an LCOV SF entry, avoiding ambiguous suffixes."""
    filename = normalize_path(filename)
    repo_relative = normalize_path(os.path.join(repo_path, filename))
    candidates = {filename, repo_relative}
    for candidate in candidates:
        if candidate in coverage_data:
            return candidate

    suffix_matches = [
        source_file for source_file in coverage_data
        if source_file.endswith("/" + filename) or filename.endswith("/" + source_file)
    ]
    return suffix_matches[0] if len(suffix_matches) == 1 else None


def calculate_incremental_coverage(repo_path, oldgit, newgit, info_path, diff_text=None):
    """Calculate coverage of Git-added lines.

    The returned object is JSON-safe and deliberately includes the individual lines,
    so the web-report generator can limit its editable controls to ``未覆盖`` lines.
    """
    repo_path = os.path.abspath(repo_path)
    if diff_text is None:
        diff_text = run_git_diff(repo_path, oldgit, newgit)
    changes = parse_diff_text(diff_text)
    coverage_data, info_files = load_lcov_info(info_path)

    details = []
    counters = defaultdict(int)
    uncovered_lines_by_file = defaultdict(list)
    for filename in sorted(changes):
        coverage_file = resolve_coverage_file(filename, coverage_data, repo_path)
        line_data = coverage_data.get(coverage_file, {}) if coverage_file else None
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
            if status == STATUS_UNCOVERED:
                uncovered_lines_by_file[filename].append(line_number)
            details.append({
                "file_path": filename,
                "coverage_file": coverage_file or "",
                "line_number": line_number,
                "execution_count": execution_count,
                "status": status,
            })

    covered = counters[STATUS_COVERED]
    uncovered = counters[STATUS_UNCOVERED]
    ignored = counters[STATUS_IGNORED]
    missing = counters[STATUS_MISSING]
    coverable_total = covered + uncovered
    changed_total = len(details)
    coverage_rate = covered * 100.0 / coverable_total if coverable_total else None
    return {
        "schema_version": 1,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "repo_path": repo_path,
        "oldgit": oldgit,
        "newgit": newgit,
        "info_files": [os.path.abspath(path) for path in info_files],
        "summary": {
            "changed_lines": changed_total,
            "covered": covered,
            "uncovered": uncovered,
            "ignored": ignored,
            "missing": missing,
            "coverable_total": coverable_total,
            "coverage_rate": coverage_rate,
        },
        "details": details,
        "uncovered_lines_by_file": {
            filename: sorted(lines) for filename, lines in sorted(uncovered_lines_by_file.items())
        },
    }


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
    """Write the familiar Details/Summary workbook without external dependencies."""
    details_rows = [["File", "Coverage File", "Line", "Execution Count", "Coverage"]]
    for item in result["details"]:
        details_rows.append([
            item["file_path"], item.get("coverage_file", ""), item["line_number"],
            item.get("execution_count"), item["status"],
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
    summary_rows.append([])
    rate = summary["coverage_rate"]
    summary_rows.append(["覆盖率(有效增量行)", "{}/{}".format(summary["covered"], total),
                         "{:.2f}%".format(rate) if rate is not None else "N/A"])
    summary_rows.append(["总增量行", changed_total, "100%" if changed_total else "N/A"])
    with open(output_path, "wb") as handle:
        handle.write(_build_simple_xlsx([("Details", details_rows), ("Summary", summary_rows)]))


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


def main():
    parser = argparse.ArgumentParser(description="Git 增量代码覆盖率检测（基于 LCOV .info 文件）")
    parser.add_argument("--repo", required=True, help="Git 仓库路径")
    parser.add_argument("--oldgit", required=True, help="旧 commit")
    parser.add_argument("--newgit", required=True, help="新 commit")
    parser.add_argument("--info", required=True, help=".info 文件或目录")
    parser.add_argument("--excel", default="coverage_result.xlsx", help="结果 Excel 文件")
    parser.add_argument("--json", dest="json_path", help="可选：结果 JSON 文件")
    parser.add_argument("--steps", nargs="+", choices=["diff", "check"], default=["diff", "check"],
                        help="兼容旧参数：diff=生成 diff.patch，check=计算覆盖率")
    args = parser.parse_args()

    diff_file = os.path.join(os.path.abspath(args.repo), "diff.patch")
    diff_text = None
    if "diff" in args.steps:
        diff_text = generate_diff_files(args.repo, args.oldgit, args.newgit, diff_file)
    if "check" in args.steps:
        if diff_text is None:
            if not os.path.exists(diff_file):
                parser.error("--steps check 需要已有 {}，或同时执行 diff".format(diff_file))
            with open(diff_file, "r", encoding="utf-8", errors="replace") as handle:
                diff_text = handle.read()
        result = calculate_incremental_coverage(args.repo, args.oldgit, args.newgit, args.info, diff_text)
        write_result_excel(result, args.excel)
        if args.json_path:
            write_result_json(result, args.json_path)
            print("[INFO] 结果 JSON 已写入 {}".format(args.json_path))
        print("[INFO] 结果已写入 {}".format(args.excel))
        print_result(result)


if __name__ == "__main__":
    main()
