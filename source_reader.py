"""
Source Reader module for OneSensor code coverage detail page.
Reads and slices source code lines, extracts function ranges using token-aware brace depth tracking,
detects basic blocks, and merges review/analysis state into unified Line DTOs.
Supports source sidecar serialization and deserialization for true lazy loading.
"""

import html
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from code_region import FunctionRange

logger = logging.getLogger(__name__)

CONTROL_FLOW_RE = re.compile(r'\b(if|else|for|while|do|switch|case|default)\b')
FUNC_ENTRY_RE = re.compile(r'^[A-Za-z_][\w\s\*]*\s+[A-Za-z_]\w*\s*\([^;]*\)\s*(\{|$)', re.M)
CONFIRMED_STATUS_SET = {'可覆盖', '无法覆盖', '冗余代码'}


def strip_html_text(value: str) -> str:
    value = re.sub(r'<[^>]+>', '', value)
    return html.unescape(value).replace('\r', '').strip()


def get_code_text(line_text: str) -> str:
    colon_index = line_text.find(':')
    return (line_text[colon_index + 1:] if colon_index >= 0 else line_text).strip()


def is_control_flow_text(code_text: str) -> bool:
    return CONTROL_FLOW_RE.search(code_text) is not None


def is_function_entry_text(code_text: str) -> bool:
    code_text = re.sub(r'/\*.*?\*/', '', code_text).strip()
    code_text = re.sub(r'\s+', ' ', code_text)
    if not code_text or code_text.endswith(';') or is_control_flow_text(code_text):
        return False
    if re.match(r'^(return|typedef|struct|enum|union)\b', code_text):
        return False
    return FUNC_ENTRY_RE.search(code_text) is not None


def strip_line_comment(code_text: str) -> str:
    return re.sub(r'//.*$', '', code_text or '').strip()


def is_jump_text(code_text: str) -> bool:
    return re.match(r'^(return|goto|break|continue)\b', strip_line_comment(code_text)) is not None


def is_structural_text(code_text: str) -> bool:
    text = strip_line_comment(code_text)
    return text == '' or re.match(r'^[{}]+;?$', text) is not None


def is_simple_auto_group_text(code_text: str) -> bool:
    text = strip_line_comment(code_text)
    text = re.sub(r'/\*.*?\*/', '', text).strip()
    if not text or is_control_flow_text(text) or is_function_entry_text(text) or is_jump_text(text):
        return False
    if re.match(r'^[{}]+;?$', text) or re.match(r'^(case\b.*:|default\s*:|[A-Za-z_]\w*\s*:)$', text):
        return False
    if not text.endswith(';'):
        return False
    has_assignment = (
        re.search(r'(^|[^=!<>])=([^=]|$)', text) is not None
        or re.search(r'(\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=|>>=)', text) is not None
    )
    is_simple_declaration = (
        re.match(
            r'^(?:const\s+|static\s+|volatile\s+|register\s+|unsigned\s+|signed\s+|struct\s+\w+\s+|enum\s+\w+\s+|union\s+\w+\s+|[A-Za-z_]\w*\s+)+[*\s]*[A-Za-z_]\w*(?:\s*=\s*[^;]+)?\s*;$',
            text,
        )
        is not None
    )
    return has_assignment or is_simple_declaration


def extract_function_name(code_text: str) -> str:
    code_text = re.sub(r'/\*.*?\*/', '', code_text).strip()
    code_text = re.sub(r'\s+', ' ', code_text)
    match = re.search(r'([A-Za-z_]\w*)\s*\([^;]*\)\s*(\{|$)', code_text)
    return match.group(1) if match else ""


def extract_report_file_path(content: str, fallback_path: str = "") -> str:
    title_match = re.search(r'<title[^>]*>(.*?)</title>', content, re.I | re.S)
    if title_match:
        title_text = strip_html_text(title_match.group(1))
        lcov_match = re.search(r'LCOV\s+-\s+.*?\s+-\s+(.+)$', title_text)
        if lcov_match:
            return lcov_match.group(1).strip()
    return fallback_path.replace(os.sep, '/').replace('.gcov.html', '')


def is_line_pending_analysis(
    coverage_state: str,
    analysis_state: str = "未确认",
    is_draft: bool = False,
    fill_status: str = "未填写",
) -> bool:
    """
    Unified Single Source of Truth for whether a line requires coverage analysis.
    A line is pending analysis if it is uncovered AND (not confirmed OR is still a draft).
    """
    if coverage_state != "uncovered":
        return False
    if is_draft:
        return True
    if fill_status == "未填写":
        return True
    return analysis_state not in CONFIRMED_STATUS_SET


class SourceLineDTO:
    """Represents a unified Code Detail Line DTO."""

    def __init__(
        self,
        line_no: int,
        source: str = "",
        raw_html: str = "",
        coverage_state: str = "ignored",  # 'covered' | 'uncovered' | 'ignored'
        analysis_state: str = "未确认",   # '未确认' | '可覆盖' | '无法覆盖' | '冗余代码' | '未填写'
        is_pending_analysis: bool = False,
        reviewer: str = "",
        coverage_method: str = "",
        uncovered_reason: str = "",
        is_draft: bool = False,
        block_start_line: Optional[int] = None,
        block_end_line: Optional[int] = None,
        block_type: str = "single",
        function_name: str = "",
        is_block_entry: bool = False,
    ):
        self.line_no = int(line_no)
        self.source = str(source)
        self.raw_html = str(raw_html)
        self.coverage_state = str(coverage_state)
        self.analysis_state = str(analysis_state)
        self.is_pending_analysis = bool(is_pending_analysis)
        self.reviewer = str(reviewer or "")
        self.coverage_method = str(coverage_method or "")
        self.uncovered_reason = str(uncovered_reason or "")
        self.is_draft = bool(is_draft)
        self.block_start_line = int(block_start_line) if block_start_line is not None else self.line_no
        self.block_end_line = int(block_end_line) if block_end_line is not None else self.line_no
        self.block_type = str(block_type or "single")
        self.function_name = str(function_name or "")
        self.is_block_entry = bool(is_block_entry)

    def to_dict(self) -> dict:
        return {
            "line_no": self.line_no,
            "source": self.source,
            "raw_html": self.raw_html,
            "coverage_state": self.coverage_state,
            "analysis_state": self.analysis_state,
            "is_pending_analysis": self.is_pending_analysis,
            "reviewer": self.reviewer,
            "coverage_method": self.coverage_method,
            "uncovered_reason": self.uncovered_reason,
            "is_draft": self.is_draft,
            "block_start_line": self.block_start_line,
            "block_end_line": self.block_end_line,
            "block_type": self.block_type,
            "function_name": self.function_name,
            "is_block_entry": self.is_block_entry,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SourceLineDTO":
        return cls(
            line_no=data.get("line_no", 0),
            source=data.get("source", ""),
            raw_html=data.get("raw_html", ""),
            coverage_state=data.get("coverage_state", "ignored"),
            analysis_state=data.get("analysis_state", "未确认"),
            is_pending_analysis=data.get("is_pending_analysis", False),
            reviewer=data.get("reviewer", ""),
            coverage_method=data.get("coverage_method", ""),
            uncovered_reason=data.get("uncovered_reason", ""),
            is_draft=data.get("is_draft", False),
            block_start_line=data.get("block_start_line"),
            block_end_line=data.get("block_end_line"),
            block_type=data.get("block_type", "single"),
            function_name=data.get("function_name", ""),
            is_block_entry=data.get("is_block_entry", False),
        )


class SourceContext:
    """Represents the parsed source context of an entire file."""

    def __init__(
        self,
        project_name: str,
        file_path: str,
        lines: List[SourceLineDTO],
        function_ranges: Optional[List[FunctionRange]] = None,
        pending_lines: Optional[List[int]] = None,
        total_uncovered_count: int = 0,
        confirmed_count: int = 0,
        report_id: str = "",
    ):
        self.project_name = project_name
        self.file_path = file_path
        self.lines = lines
        self.function_ranges = function_ranges or []
        self.pending_lines = pending_lines or []
        self.total_uncovered_count = total_uncovered_count or sum(
            1 for line in lines if line.coverage_state == "uncovered"
        )
        self.confirmed_count = confirmed_count or sum(
            1 for line in lines
            if line.coverage_state == "uncovered"
            and not line.is_draft
            and line.analysis_state in CONFIRMED_STATUS_SET
        )
        self.report_id = report_id

    @property
    def total_lines(self) -> int:
        return len(self.lines)

    def get_line(self, line_no: int) -> Optional[SourceLineDTO]:
        if 1 <= line_no <= len(self.lines):
            return self.lines[line_no - 1]
        return None

    def to_dict(self) -> dict:
        return {
            "project_name": self.project_name,
            "file_path": self.file_path,
            "report_id": self.report_id,
            "total_lines": self.total_lines,
            "total_uncovered_count": self.total_uncovered_count,
            "confirmed_count": self.confirmed_count,
            "pending_lines": self.pending_lines,
            "function_ranges": [f.to_dict() for f in self.function_ranges],
            "lines": [l.to_dict() for l in self.lines],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SourceContext":
        lines = [SourceLineDTO.from_dict(d) for d in data.get("lines", [])]
        function_ranges = [
            FunctionRange(f["start_line"], f["end_line"], f.get("name"))
            for f in data.get("function_ranges", [])
        ]
        return cls(
            project_name=data.get("project_name", ""),
            file_path=data.get("file_path", ""),
            lines=lines,
            function_ranges=function_ranges,
            pending_lines=data.get("pending_lines", []),
            total_uncovered_count=data.get("total_uncovered_count", 0),
            confirmed_count=data.get("confirmed_count", 0),
            report_id=data.get("report_id", ""),
        )


def extract_c_function_ranges(raw_lines: List[dict]) -> List[FunctionRange]:
    """
    Extract accurate C/C++ function ranges using token-aware brace depth tracking.
    Correctly ignores braces in comments, strings, char literals, and macros.
    """
    functions: List[FunctionRange] = []
    in_block_comment = False
    current_fn_name = None
    current_fn_start_line = None
    brace_depth = 0
    entered_body = False

    for idx, item in enumerate(raw_lines):
        line_no = item["line_no"]
        text = item["code_text"]

        # Clean string, char literals and comments to count real code braces
        i = 0
        n = len(text)
        line_cleaned = []

        while i < n:
            if in_block_comment:
                if i + 1 < n and text[i:i+2] == '*/':
                    in_block_comment = False
                    i += 2
                else:
                    i += 1
                continue

            if i + 1 < n and text[i:i+2] == '/*':
                in_block_comment = True
                i += 2
                continue

            if i + 1 < n and text[i:i+2] == '//':
                break

            char = text[i]

            if char == '"':
                i += 1
                while i < n:
                    if text[i] == '\\':
                        i += 2
                    elif text[i] == '"':
                        i += 1
                        break
                    else:
                        i += 1
                continue

            if char == "'":
                i += 1
                while i < n:
                    if text[i] == '\\':
                        i += 2
                    elif text[i] == "'":
                        i += 1
                        break
                    else:
                        i += 1
                continue

            line_cleaned.append(char)
            i += 1

        clean_text = "".join(line_cleaned).strip()

        if current_fn_start_line is None:
            if is_function_entry_text(text):
                fn_name = extract_function_name(text)
                current_fn_name = fn_name
                current_fn_start_line = line_no
                open_b = clean_text.count('{')
                close_b = clean_text.count('}')
                brace_depth = open_b - close_b
                entered_body = (open_b > 0)
                if entered_body and brace_depth <= 0:
                    # Single-line function
                    functions.append(FunctionRange(current_fn_start_line, line_no, current_fn_name))
                    current_fn_name = None
                    current_fn_start_line = None
                    brace_depth = 0
                    entered_body = False
        else:
            open_b = clean_text.count('{')
            close_b = clean_text.count('}')
            if not entered_body:
                if open_b > 0:
                    entered_body = True
                    brace_depth = open_b - close_b
                elif is_function_entry_text(text) and not clean_text.startswith('('):
                    # Previous function signature had no body
                    fn_name = extract_function_name(text)
                    functions.append(FunctionRange(current_fn_start_line, raw_lines[idx - 1]["line_no"], current_fn_name))
                    current_fn_name = fn_name
                    current_fn_start_line = line_no
                    brace_depth = open_b - close_b
                    entered_body = (open_b > 0)
            else:
                brace_depth += (open_b - close_b)

            if entered_body and brace_depth <= 0:
                functions.append(FunctionRange(current_fn_start_line, line_no, current_fn_name))
                current_fn_name = None
                current_fn_start_line = None
                brace_depth = 0
                entered_body = False

    if current_fn_start_line is not None and raw_lines:
        functions.append(FunctionRange(current_fn_start_line, raw_lines[-1]["line_no"], current_fn_name))

    return functions


def parse_source_lines_from_gcov_html(
    content: str,
    project_name: str = "",
    file_path: str = "",
    analysis_records: Optional[List[Dict[str, Any]]] = None,
    review_scope: str = "full",
    incremental_line_numbers: Optional[Set[int]] = None,
    report_id: str = "",
) -> SourceContext:
    r"""
    Parse an LCOV HTML report (.gcov.html) into a structured SourceContext.
    Handles both modern (id="L\d+") and legacy (<span class="lineNum">) genhtml templates.
    """
    if not file_path:
        file_path = extract_report_file_path(content, "source_file")

    analysis_map = {}
    if analysis_records:
        for rec in analysis_records:
            l_num = rec.get("line_number")
            if l_num is not None:
                analysis_map[int(l_num)] = rec

    raw_lines = []
    # Fast pattern detection
    if 'id="L' in content or "id='L" in content:
        # Modern genhtml pattern: <span id="L1" ...>
        modern_pattern = re.compile(r'<span\b[^>]*\bid=["\']L(\d+)["\'][^>]*>(.*?)</span>', re.S | re.I)
        for match in modern_pattern.finditer(content):
            line_no = int(match.group(1))
            inner_html = match.group(2)
            line_text = strip_html_text(inner_html)
            code_text = get_code_text(line_text)

            full_tag = match.group(0)
            is_cov = ("tlaGNC" in full_tag or "tlaBgGNC" in full_tag or "lineCov" in full_tag)
            is_uncov = ("tlaUNC" in full_tag or "tlaBgUNC" in full_tag or "lineNoCov" in full_tag)
            is_inc = 'data-coverage-review="incremental"' in full_tag or (
                incremental_line_numbers is not None and line_no in incremental_line_numbers
            )

            cov_state = "uncovered" if is_uncov else ("covered" if is_cov else "ignored")

            raw_lines.append({
                "line_no": line_no,
                "code_text": code_text,
                "raw_html": inner_html.strip(),
                "coverage_state": cov_state,
                "is_uncovered": is_uncov,
                "is_incremental": is_inc,
            })
    elif '<span class="lineNum">' in content:
        legacy_pattern = re.compile(r'<span class="lineNum">\s*(\d+)\s*</span>(.*?)(?=<span class="lineNum">|</pre>)', re.S)
        for match in legacy_pattern.finditer(content):
            line_no = int(match.group(1))
            tail = match.group(2)
            line_text = strip_html_text(tail)
            code_text = get_code_text(line_text)

            is_cov = ("lineCov" in tail or "tlaGNC" in tail or "tlaBgGNC" in tail)
            is_uncov = ("lineNoCov" in tail or "tlaUNC" in tail or "tlaBgUNC" in tail)
            is_inc = 'data-coverage-review="incremental"' in tail or (
                incremental_line_numbers is not None and line_no in incremental_line_numbers
            )

            cov_state = "uncovered" if is_uncov else ("covered" if is_cov else "ignored")

            raw_lines.append({
                "line_no": line_no,
                "code_text": code_text,
                "raw_html": tail.strip(),
                "coverage_state": cov_state,
                "is_uncovered": is_uncov,
                "is_incremental": is_inc,
            })
    else:
        # Fallback: line-by-line raw text parsing
        plain_lines = content.splitlines()
        for idx, pl in enumerate(plain_lines, start=1):
            raw_lines.append({
                "line_no": idx,
                "code_text": pl.strip(),
                "raw_html": pl,
                "coverage_state": "ignored",
                "is_uncovered": False,
                "is_incremental": False,
            })

    # Ensure continuous 1..N
    raw_lines.sort(key=lambda item: item["line_no"])

    # Extract accurate function ranges via brace-aware parser
    function_ranges = extract_c_function_ranges(raw_lines)

    # Attach function_name to lines efficiently O(N)
    line_map = {item["line_no"]: item for item in raw_lines}
    for fn in function_ranges:
        for l_num in range(fn.start_line, fn.end_line + 1):
            if l_num in line_map:
                line_map[l_num]["function_name"] = fn.name

    # Group uncovered lines into basic blocks
    block_map = {}  # line_no -> (block_start_line, block_end_line, block_type, is_block_entry)
    counted = set()

    for index, item in enumerate(raw_lines):
        line_no = item["line_no"]
        is_uncov = item["is_uncovered"]
        if review_scope == "incremental" and not item["is_incremental"]:
            is_uncov = False

        if not is_uncov or line_no in counted:
            continue

        if is_control_flow_text(item["code_text"]):
            block_map[line_no] = (line_no, line_no, "control_flow", True)
            counted.add(line_no)
        else:
            # Build semantic block
            block_lines = [item]
            start_is_fn = is_function_entry_text(item["code_text"])
            consumed_until = index

            for j in range(index + 1, len(raw_lines)):
                next_item = raw_lines[j]
                if next_item["coverage_state"] == "covered":
                    break

                next_is_uncov = next_item["is_uncovered"]
                if review_scope == "incremental" and not next_item["is_incremental"]:
                    next_is_uncov = False

                if next_is_uncov:
                    if is_control_flow_text(next_item["code_text"]) or is_function_entry_text(next_item["code_text"]):
                        break
                    if start_is_fn and not is_simple_auto_group_text(next_item["code_text"]):
                        break
                    if not start_is_fn and (
                        not is_simple_auto_group_text(item["code_text"])
                        or not is_simple_auto_group_text(next_item["code_text"])
                    ):
                        break
                    block_lines.append(next_item)
                    consumed_until = j
                    continue

                if not start_is_fn:
                    break
                if is_control_flow_text(next_item["code_text"]) or is_function_entry_text(next_item["code_text"]):
                    break
                if start_is_fn and not is_structural_text(next_item["code_text"]):
                    continue
                if not is_structural_text(next_item["code_text"]):
                    break

            start_l = block_lines[0]["line_no"]
            end_l = block_lines[-1]["line_no"]
            b_type = "function_body" if start_is_fn else "sequential"
            for b_idx, bl in enumerate(block_lines):
                bl_no = bl["line_no"]
                block_map[bl_no] = (start_l, end_l, b_type, b_idx == 0)
                counted.add(bl_no)

    # Build SourceLineDTO list
    dto_lines: List[SourceLineDTO] = []
    pending_lines: List[int] = []
    total_uncovered_count = 0
    confirmed_count = 0

    for item in raw_lines:
        line_no = item["line_no"]
        cov_state = item["coverage_state"]
        if review_scope == "incremental" and not item["is_incremental"] and cov_state == "uncovered":
            cov_state = "ignored"

        if cov_state == "uncovered":
            total_uncovered_count += 1

        rec = analysis_map.get(line_no)
        analysis_state = rec.get("status") if rec else "未确认"
        is_draft = bool(rec.get("is_draft", False)) if rec else False
        fill_status = rec.get("fill_status", "已填写" if rec and rec.get("status") else "未填写") if rec else "未填写"
        reviewer = rec.get("reviewer", "") if rec else ""
        coverage_method = rec.get("coverage_method", "") if rec else ""
        uncovered_reason = rec.get("uncovered_reason", "") if rec else ""

        is_pending = is_line_pending_analysis(
            coverage_state=cov_state,
            analysis_state=analysis_state,
            is_draft=is_draft,
            fill_status=fill_status,
        )

        if is_pending:
            pending_lines.append(line_no)
        elif cov_state == "uncovered" and analysis_state in CONFIRMED_STATUS_SET and not is_draft:
            confirmed_count += 1

        b_start, b_end, b_type, is_entry = block_map.get(
            line_no, (line_no, line_no, "single", cov_state == "uncovered")
        )

        dto_lines.append(
            SourceLineDTO(
                line_no=line_no,
                source=item["code_text"],
                raw_html=item["raw_html"],
                coverage_state=cov_state,
                analysis_state=analysis_state,
                is_pending_analysis=is_pending,
                reviewer=reviewer,
                coverage_method=coverage_method,
                uncovered_reason=uncovered_reason,
                is_draft=is_draft,
                block_start_line=b_start,
                block_end_line=b_end,
                block_type=b_type,
                function_name=item.get("function_name", ""),
                is_block_entry=is_entry,
            )
        )

    return SourceContext(
        project_name=project_name,
        file_path=file_path,
        lines=dto_lines,
        function_ranges=function_ranges,
        pending_lines=pending_lines,
        total_uncovered_count=total_uncovered_count,
        confirmed_count=confirmed_count,
        report_id=report_id,
    )


def read_source_lines(
    source_context: SourceContext, start_line: int, end_line: int
) -> List[Dict[str, Any]]:
    """
    Read line DTOs for a single contiguous range [start_line, end_line].
    Validates boundary conditions and returns serialized line dictionaries.
    """
    total = source_context.total_lines
    start_line = int(start_line)
    end_line = int(end_line)

    if start_line <= 0 or end_line <= 0:
        raise ValueError(f"Line numbers must be positive: start={start_line}, end={end_line}")
    if start_line > end_line:
        raise ValueError(f"start_line ({start_line}) cannot be greater than end_line ({end_line})")
    if total > 0 and (start_line > total or end_line > total):
        raise ValueError(f"Requested range [{start_line}..{end_line}] exceeds total lines {total}")

    sliced_dtos = source_context.lines[start_line - 1:end_line]
    return [dto.to_dict() for dto in sliced_dtos]


def read_source_ranges(
    source_context: SourceContext,
    ranges: List[Dict[str, int]],
    max_ranges: int = 1000,
) -> List[Dict[str, Any]]:
    """
    Read line DTOs for multiple ranges in a single batch.
    Returns: [{'start_line': s, 'end_line': e, 'lines': [SourceLineDTO.to_dict(), ...]}, ...]
    """
    if not ranges:
        return []
    if len(ranges) > max_ranges:
        raise ValueError(f"Requested ranges count {len(ranges)} exceeds maximum allowed {max_ranges}")

    results = []
    for r in ranges:
        s_line = int(r.get("start_line", 0))
        e_line = int(r.get("end_line", 0))
        lines_data = read_source_lines(source_context, s_line, e_line)
        results.append({
            "start_line": s_line,
            "end_line": e_line,
            "lines": lines_data,
        })
    return results


def save_source_sidecar(
    output_dir: str,
    report_id: str,
    file_path_hash: str,
    source_context: SourceContext,
) -> str:
    """Serialize SourceContext to a server-side sidecar file in .source_cache directory."""
    cache_dir = os.path.join(output_dir, ".source_cache", report_id)
    os.makedirs(cache_dir, exist_ok=True)
    sidecar_path = os.path.join(cache_dir, f"{file_path_hash}.source.json")
    with open(sidecar_path, "w", encoding="utf-8") as f:
        json.dump(source_context.to_dict(), f, ensure_ascii=False)
    return sidecar_path


def load_source_sidecar(
    output_dir: str,
    report_id: str,
    file_path_hash: str,
) -> Optional[SourceContext]:
    """Load SourceContext from a server-side sidecar file if present."""
    sidecar_path = os.path.join(output_dir, ".source_cache", report_id, f"{file_path_hash}.source.json")
    if os.path.isfile(sidecar_path):
        try:
            with open(sidecar_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return SourceContext.from_dict(data)
        except Exception as e:
            logger.warning(f"[SourceReader] Failed to load sidecar {sidecar_path}: {e}")
    return None
