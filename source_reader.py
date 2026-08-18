"""
Source Reader module for OneSensor code coverage detail page.
Reads and slices source code lines, extracts function ranges, basic blocks,
and merges review/analysis state into unified Line DTOs.
"""

import html
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


class SourceContext:
    """Holds parsed source code information, function ranges, and line DTOs."""

    def __init__(
        self,
        project_name: str,
        file_path: str,
        lines: List[SourceLineDTO],
        function_ranges: Optional[List[FunctionRange]] = None,
        pending_lines: Optional[List[int]] = None,
    ):
        self.project_name = str(project_name)
        self.file_path = str(file_path)
        self.lines = lines or []
        self.total_lines = len(self.lines)
        self.function_ranges = function_ranges or []
        if pending_lines is not None:
            self.pending_lines = sorted(set(pending_lines))
        else:
            self.pending_lines = [
                line.line_no for line in self.lines if line.is_pending_analysis
            ]

    def get_line(self, line_no: int) -> Optional[SourceLineDTO]:
        if 1 <= line_no <= len(self.lines):
            return self.lines[line_no - 1]
        return None


def parse_source_lines_from_gcov_html(
    content: str,
    project_name: str = "",
    file_path: str = "",
    analysis_records: Optional[List[Dict[str, Any]]] = None,
    review_scope: str = "full",
    incremental_line_numbers: Optional[Set[int]] = None,
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
    if 'id="L' in content or 'id=\'L' in content:
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

    # Extract function ranges
    function_ranges: List[FunctionRange] = []
    current_fn_start = None
    for index, item in enumerate(raw_lines):
        if is_function_entry_text(item["code_text"]):
            if current_fn_start is not None:
                fn_name = extract_function_name(raw_lines[current_fn_start]["code_text"])
                function_ranges.append(
                    FunctionRange(
                        raw_lines[current_fn_start]["line_no"],
                        raw_lines[index - 1]["line_no"],
                        fn_name,
                    )
                )
            current_fn_start = index
    if current_fn_start is not None and raw_lines:
        fn_name = extract_function_name(raw_lines[current_fn_start]["code_text"])
        function_ranges.append(
            FunctionRange(
                raw_lines[current_fn_start]["line_no"],
                raw_lines[-1]["line_no"],
                fn_name,
            )
        )

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

        block = [item]
        b_type = (
            "function_entry"
            if is_function_entry_text(item["code_text"])
            else "control_flow"
            if is_control_flow_text(item["code_text"])
            else "single"
        )

        if b_type != "control_flow":
            if b_type == "single":
                b_type = "straight_line"
            for next_item in raw_lines[index + 1:]:
                if is_control_flow_text(next_item["code_text"]) or is_function_entry_text(next_item["code_text"]):
                    break
                next_uncov = next_item["is_uncovered"]
                if review_scope == "incremental" and not next_item["is_incremental"]:
                    next_uncov = False

                if next_uncov:
                    if b_type == "function_entry" and not is_simple_auto_group_text(next_item["code_text"]):
                        break
                    if b_type != "function_entry" and (
                        not is_simple_auto_group_text(item["code_text"])
                        or not is_simple_auto_group_text(next_item["code_text"])
                    ):
                        break
                    block.append(next_item)
                    continue
                if b_type != "function_entry":
                    break
                if not is_structural_text(next_item["code_text"]):
                    break

        b_start = block[0]["line_no"]
        b_end = block[-1]["line_no"]
        for b_idx, b_item in enumerate(block):
            b_lno = b_item["line_no"]
            counted.add(b_lno)
            block_map[b_lno] = (b_start, b_end, b_type, b_idx == 0)

    # Build final SourceLineDTO list
    final_lines: List[SourceLineDTO] = []
    pending_lines: List[int] = []

    for item in raw_lines:
        line_no = item["line_no"]
        b_info = block_map.get(line_no, (line_no, line_no, "single", False))
        b_start, b_end, b_type, is_entry = b_info

        # Check DB analysis record
        db_rec = analysis_map.get(line_no, {})
        status = db_rec.get("status") or "未确认"
        reviewer = db_rec.get("reviewer") or ""
        is_draft = bool(db_rec.get("is_draft", False))
        cov_method = db_rec.get("coverage_method") or ""
        uncov_reason = db_rec.get("uncovered_reason") or ""

        # Determine pending analysis:
        # Line is uncovered (or incremental uncovered) AND not confirmed in DB
        line_is_uncovered = item["is_uncovered"]
        if review_scope == "incremental":
            line_is_uncovered = line_is_uncovered and item["is_incremental"]

        is_pending = False
        if line_is_uncovered:
            if status not in CONFIRMED_STATUS_SET or is_draft:
                is_pending = True

        if is_pending:
            pending_lines.append(line_no)

        final_lines.append(
            SourceLineDTO(
                line_no=line_no,
                source=item["code_text"],
                raw_html=item["raw_html"],
                coverage_state=item["coverage_state"],
                analysis_state=status,
                is_pending_analysis=is_pending,
                reviewer=reviewer,
                coverage_method=cov_method,
                uncovered_reason=uncov_reason,
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
        lines=final_lines,
        function_ranges=function_ranges,
        pending_lines=pending_lines,
    )


def read_source_lines(
    source_context: SourceContext,
    start_line: int,
    end_line: int,
) -> List[Dict[str, Any]]:
    """
    Read code lines in range [start_line, end_line] (1-indexed inclusive).
    Validates range against total_lines.
    """
    if not source_context:
        raise ValueError("source_context is required")

    total = source_context.total_lines
    start_line = int(start_line)
    end_line = int(end_line)

    if start_line <= 0 or end_line <= 0:
        raise ValueError(f"Invalid line range: start={start_line}, end={end_line} (must be > 0)")
    if start_line > end_line:
        raise ValueError(f"Invalid line range: start={start_line} > end={end_line}")
    if total > 0 and (start_line > total or end_line > total):
        raise ValueError(f"Line range [{start_line}, {end_line}] exceeds total lines {total}")

    # 1-indexed slice
    sliced = source_context.lines[start_line - 1:end_line]
    return [line.to_dict() for line in sliced]


def read_source_ranges(
    source_context: SourceContext,
    ranges: List[Dict[str, int]],
    max_ranges: int = 100,
) -> List[Dict[str, Any]]:
    """
    Read multiple code ranges in one operation.
    Validates maximum range count and each individual range.
    """
    if not isinstance(ranges, list):
        raise ValueError("ranges must be a list")
    if len(ranges) > max_ranges:
        raise ValueError(f"Too many ranges requested (max={max_ranges})")

    result = []
    for r in ranges:
        if not isinstance(r, dict):
            raise ValueError("Each range item must be an object with start_line and end_line")
        s_line = int(r.get("start_line", 0))
        e_line = int(r.get("end_line", 0))
        lines = read_source_lines(source_context, s_line, e_line)
        result.append({
            "start_line": s_line,
            "end_line": e_line,
            "lines": lines,
        })
    return result
