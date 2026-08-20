"""
Source Reader module for code coverage detail page.
Parses, sanitizes, and serializes GCOV HTML source lines into unified DTOs
and server-side sidecars.
Reads and slices source code lines, extracts function ranges using token-aware brace depth tracking,
detects basic blocks, and merges review/analysis state into unified Line DTOs.
Supports source sidecar serialization and deserialization for true lazy loading.
"""

import hashlib
import html
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from code_region import FunctionRange

logger = logging.getLogger(__name__)

CONTROL_FLOW_RE = re.compile(r'\b(if|else|for|while|do|switch|case|default|catch)\b')
CONFIRMED_STATUS_SET = {'可覆盖', '无法覆盖', '冗余代码'}


def calc_sidecar_file_key(file_path: str) -> str:
    """Compute stable SHA-256 hash (32 hex chars) of normalized file path for sidecar indexing."""
    normalized = str(file_path or "").replace("\\", "/").strip().lstrip("/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]

def compute_db_file_path_hash(file_path: str) -> str:
    """Historical DB identity: MD5(normalized path), 32 hex characters."""
    normalized = str(file_path or "").replace("\\", "/").strip().lstrip("/")
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()

# Compatibility name for old DB callers.  It must never be the sidecar key.
compute_file_path_hash = compute_db_file_path_hash


def is_valid_report_id(report_id: str) -> bool:
    """Validate report_id format (e.g. report_abc123 or alphanumeric with underscore/hyphen)."""
    if not report_id:
        return True
    return bool(re.match(r'^[A-Za-z0-9_-]{1,64}$', str(report_id).strip()))


def is_valid_review_scope(scope: str) -> bool:
    """Validate review_scope is 'full' or 'incremental'."""
    return scope in ("full", "incremental")


def strip_html_text(value: str) -> str:
    value = re.sub(r'<[^>]+>', '', value)
    return html.unescape(value).replace('\r', '').strip()


def get_code_text(line_text: str) -> str:
    colon_index = line_text.find(':')
    return (line_text[colon_index + 1:] if colon_index >= 0 else line_text).strip()


def is_control_flow_text(code_text: str) -> bool:
    return CONTROL_FLOW_RE.search(code_text) is not None


def extract_function_name_from_signature(sig_text: str) -> str:
    """
    Extract function name from C/C++ function signature.
    Supports:
    - Standard C functions (int foo(int a))
    - C++ scoped names (Namespace::Class::func)
    - Constructors and Destructors (Foo::Foo, Foo::~Foo)
    - Operators (operator==, operator(), operator new, Foo::operator())
    - GNU attributes (__attribute__((noinline)) int foo(int a))
    - C++11 attributes ([[nodiscard]] int foo(int a))
    - Templates (template <typename T> T Foo<T>::bar(const T& x))
    - Trailing return types (auto func() -> type)
    - Qualifiers (const, noexcept, override)
    """
    sig_text = re.sub(r'/\*.*?\*/', '', sig_text).strip()
    sig_text = re.sub(r'//.*$', '', sig_text).strip()

    # Strip GNU __attribute__((...)), __declspec(...), and C++11 [[...]] attributes
    sig_text = re.sub(r'__attribute__\s*\(\(.*?\)\)', '', sig_text)
    sig_text = re.sub(r'__declspec\s*\([^\)]*\)', '', sig_text)
    sig_text = re.sub(r'\[\[.*?\]\]', '', sig_text)
    sig_text = sig_text.strip()

    # Strip leading template <...> if present
    sig_text = re.sub(r'^template\s*<.*?>\s*', '', sig_text)

    # Check for C++ operator overloads first (especially operator(), operator[], Foo::operator())
    op_match = re.search(r'((?:[~A-Za-z_]\w*(?:<[^>]*>)?\s*::\s*)*operator\s*(?:\(\)|\[\]|->|\+\+|--|[^\s\(]+))\s*\(', sig_text)
    if op_match:
        raw_op = op_match.group(1)
        return re.sub(r'\s+', '', raw_op)

    paren_idx = sig_text.find('(')
    if paren_idx < 0:
        return ""
    prefix = sig_text[:paren_idx].strip()
    if not prefix:
        return ""

    # Match identifier or C++ qualified name (e.g., Foo::bar, ~Foo, Foo::Foo, ns::Class::method, Foo<T>::bar)
    match = re.search(r'([~A-Za-z_]\w*(?:<[^>]*>)?(?:\s*::\s*[~A-Za-z_]\w*(?:<[^>]*>)?)*)\s*$', prefix)
    if match:
        name = re.sub(r'\s+', '', match.group(1))
        # Exclude language keywords
        if name in {
            'if', 'for', 'while', 'switch', 'catch', 'do', 'return',
            'sizeof', 'typeof', 'alignas', 'decltype', 'static_assert',
            'typedef', 'struct', 'class', 'enum', 'union', 'namespace', 'using'
        }:
            return ""
        return name
    return ""


def is_function_entry_text(code_text: str) -> bool:
    code_text = re.sub(r'/\*.*?\*/', '', code_text).strip()
    code_text = re.sub(r'//.*$', '', code_text).strip()
    if not code_text or code_text.endswith(';') or is_control_flow_text(code_text):
        return False
    if re.match(r'^(typedef|struct|enum|union|using|namespace|return)\b', code_text):
        return False
    name = extract_function_name_from_signature(code_text)
    return bool(name)


def strip_line_comment(text: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    i = 0
    while i < len(text):
        c = text[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if c == '\\':
            escaped = True
            i += 1
            continue
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if c == '/' and i + 1 < len(text) and text[i + 1] == '/':
                return text[:i].rstrip()
        i += 1
    return text


def is_jump_text(code_text: str) -> bool:
    return re.search(r'\b(return|goto|break|continue|throw)\b', code_text) is not None


def is_simple_auto_group_text(code_text: str) -> bool:
    text = strip_line_comment(code_text)
    text = re.sub(r'/\*.*?\*/', '', text).strip()
    if not text or is_control_flow_text(text) or is_function_entry_text(text) or is_jump_text(text):
        return False
    if re.match(r'^[{}]+;?$', text) or re.match(r'^(case\b.*:|default\s*:|[A-Za-z_]\w*\s*:)$', text):
        return False
    if not text.endswith(';'):
        return False
    has_assignment = re.search(r'(^|[^=!<>])=([^=]|$)', text) is not None or re.search(r'(\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=|>>=)', text) is not None
    is_simple_declaration = re.match(r'^(?:const\s+|static\s+|volatile\s+|register\s+|unsigned\s+|signed\s+|struct\s+\w+\s+|enum\s+\w+\s+|union\s+\w+\s+|[A-Za-z_]\w*\s+)+[*\s]*[A-Za-z_]\w*(?:\s*=\s*[^;]+)?\s*;$', text) is not None
    return has_assignment or is_simple_declaration


def extract_function_name(code_text: str) -> str:
    return extract_function_name_from_signature(code_text)


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
        suggested_reviewer: str = "",
    ):
        self.line_no = int(line_no)
        self.source = str(source)
        self.raw_html = str(raw_html)
        self.coverage_state = str(coverage_state)
        self.analysis_state = str(analysis_state)
        self.is_pending_analysis = bool(is_pending_analysis)
        # Static Git blame suggestion.  This is intentionally separate from
        # ``reviewer`` because the latter is the user/database fact.
        self.suggested_reviewer = str(suggested_reviewer or "")
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
            "suggested_reviewer": self.suggested_reviewer,
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
            suggested_reviewer=data.get("suggested_reviewer", ""),
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
        self.function_ranges = []
        for raw_range in function_ranges or []:
            if isinstance(raw_range, FunctionRange):
                self.function_ranges.append(raw_range)
            elif isinstance(raw_range, dict):
                self.function_ranges.append(FunctionRange(
                    raw_range.get("start_line", 0),
                    raw_range.get("end_line", 0),
                    raw_range.get("name"),
                ))
            elif isinstance(raw_range, (list, tuple)) and len(raw_range) >= 2:
                self.function_ranges.append(FunctionRange(
                    raw_range[0], raw_range[1], raw_range[2] if len(raw_range) > 2 else None
                ))
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
    Extract accurate C/C++ function ranges using signature accumulation and
    token-aware brace depth tracking.
    Correctly handles:
    - Multi-line parameter lists
    - C++ scoped names (Namespace::Class::func)
    - Constructors and destructors (Foo::Foo, Foo::~Foo)
    - Trailing return types (auto func() -> type)
    - Const / noexcept / override qualifiers
    - Braces in comments, strings, char literals, and macros
    """
    functions: List[FunctionRange] = []
    in_block_comment = False

    state = 'SEEKING'  # 'SEEKING', 'IN_SIGNATURE', 'WAITING_FOR_BODY', 'IN_BODY'
    sig_start_line = None
    sig_text_buffer = []
    paren_depth = 0
    brace_depth = 0
    current_fn_name = ""
    candidate_prefix_lines = []

    for idx, item in enumerate(raw_lines):
        line_no = item["line_no"]
        text = item["code_text"]

        # Clean string, char literals and comments to get structural tokens
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
        is_macro = clean_text.startswith('#')

        if state == 'SEEKING':
            if not clean_text:
                continue
            if is_macro:
                candidate_prefix_lines = []
                continue

            if '(' in clean_text:
                first_paren = clean_text.find('(')
                prefix = clean_text[:first_paren].strip()
                if (
                    re.match(r'^(typedef|struct|enum|union|using|namespace|return|goto|break|continue)\b', prefix)
                    or is_control_flow_text(prefix)
                ):
                    candidate_prefix_lines = []
                    continue

                combined_sig = " ".join([p[1] for p in candidate_prefix_lines] + [clean_text]) if candidate_prefix_lines else clean_text
                fn_name = extract_function_name_from_signature(combined_sig) or extract_function_name_from_signature(clean_text)
                if not fn_name:
                    candidate_prefix_lines = []
                    continue

                sig_start_line = candidate_prefix_lines[0][0] if candidate_prefix_lines else line_no
                sig_text_buffer = [p[1] for p in candidate_prefix_lines] + [clean_text] if candidate_prefix_lines else [clean_text]
                candidate_prefix_lines = []
                current_fn_name = fn_name
                paren_depth = clean_text.count('(') - clean_text.count(')')

                if paren_depth > 0:
                    state = 'IN_SIGNATURE'
                else:
                    after_paren = clean_text[clean_text.rfind(')'):]
                    if ';' in after_paren:
                        state = 'SEEKING'
                        sig_start_line = None
                        sig_text_buffer = []
                        current_fn_name = ""
                    elif '{' in after_paren:
                        open_b = clean_text.count('{')
                        close_b = clean_text.count('}')
                        brace_depth = open_b - close_b
                        if brace_depth <= 0:
                            functions.append(FunctionRange(sig_start_line, line_no, current_fn_name))
                            state = 'SEEKING'
                            sig_start_line = None
                            sig_text_buffer = []
                            current_fn_name = ""
                        else:
                            state = 'IN_BODY'
                    else:
                        state = 'WAITING_FOR_BODY'
            else:
                if ';' in clean_text or '{' in clean_text or '}' in clean_text or clean_text.endswith(':') or is_control_flow_text(clean_text):
                    candidate_prefix_lines = []
                else:
                    if not candidate_prefix_lines or (line_no - candidate_prefix_lines[-1][0] <= 2):
                        candidate_prefix_lines.append((line_no, clean_text))
                    else:
                        candidate_prefix_lines = [(line_no, clean_text)]

        elif state == 'IN_SIGNATURE':
            if is_macro:
                continue
            sig_text_buffer.append(clean_text)
            paren_depth += (clean_text.count('(') - clean_text.count(')'))

            if paren_depth <= 0:
                full_sig = " ".join(sig_text_buffer)
                if not current_fn_name:
                    current_fn_name = extract_function_name_from_signature(full_sig)

                after_paren = clean_text[clean_text.rfind(')'):] if ')' in clean_text else clean_text
                if ';' in after_paren:
                    state = 'SEEKING'
                    sig_start_line = None
                    sig_text_buffer = []
                    current_fn_name = ""
                elif '{' in after_paren:
                    open_b = clean_text.count('{')
                    close_b = clean_text.count('}')
                    brace_depth = open_b - close_b
                    if brace_depth <= 0:
                        functions.append(FunctionRange(sig_start_line, line_no, current_fn_name))
                        state = 'SEEKING'
                        sig_start_line = None
                        sig_text_buffer = []
                        current_fn_name = ""
                    else:
                        state = 'IN_BODY'
                else:
                    state = 'WAITING_FOR_BODY'

        elif state == 'WAITING_FOR_BODY':
            if not clean_text or is_macro:
                continue
            if ';' in clean_text and '{' not in clean_text:
                state = 'SEEKING'
                sig_start_line = None
                sig_text_buffer = []
                current_fn_name = ""
            elif '{' in clean_text:
                open_b = clean_text.count('{')
                close_b = clean_text.count('}')
                brace_depth = open_b - close_b
                if brace_depth <= 0:
                    functions.append(FunctionRange(sig_start_line, line_no, current_fn_name))
                    state = 'SEEKING'
                    sig_start_line = None
                    sig_text_buffer = []
                    current_fn_name = ""
                else:
                    state = 'IN_BODY'
            else:
                if line_no - sig_start_line > 30:
                    state = 'SEEKING'
                    sig_start_line = None
                    sig_text_buffer = []
                    current_fn_name = ""

        elif state == 'IN_BODY':
            open_b = clean_text.count('{')
            close_b = clean_text.count('}')
            brace_depth += (open_b - close_b)

            if brace_depth <= 0:
                functions.append(FunctionRange(sig_start_line, line_no, current_fn_name))
                state = 'SEEKING'
                sig_start_line = None
                sig_text_buffer = []
                current_fn_name = ""
                brace_depth = 0

    if state == 'IN_BODY' and sig_start_line is not None and raw_lines:
        functions.append(FunctionRange(sig_start_line, raw_lines[-1]["line_no"], current_fn_name))

    return functions


def _validated_known_function_ranges(known_function_ranges, total_lines: int):
    """Return trusted LCOV ranges or ``None`` to request source fallback."""
    if not known_function_ranges:
        return None

    ranges = []
    for raw_range in known_function_ranges:
        try:
            if isinstance(raw_range, FunctionRange):
                start = raw_range.start_line
                end = raw_range.end_line
                name = raw_range.name
            elif isinstance(raw_range, dict):
                start = int(raw_range.get("start_line"))
                end = int(raw_range.get("end_line"))
                name = raw_range.get("name")
            elif isinstance(raw_range, (list, tuple)) and len(raw_range) >= 2:
                start = int(raw_range[0])
                end = int(raw_range[1])
                name = raw_range[2] if len(raw_range) > 2 else None
            else:
                return None
        except (TypeError, ValueError):
            return None
        if start < 1 or start > end or end > total_lines:
            return None
        ranges.append(FunctionRange(start, end, name))

    if not ranges:
        return None

    # Remove exact aliases and nested compiler-generated ranges while
    # rejecting true crossings that would create ambiguous line ownership.
    deduped = {}
    for item in ranges:
        key = (item.start_line, item.end_line)
        previous = deduped.get(key)
        if previous is None or (not previous.name and item.name):
            deduped[key] = item
    ordered = sorted(deduped.values(), key=lambda item: (item.start_line, -item.end_line))
    outermost = []
    for item in ordered:
        is_nested = False
        for existing in outermost:
            if existing.start_line <= item.start_line and item.end_line <= existing.end_line:
                is_nested = True
                break
            if (item.start_line < existing.start_line <= item.end_line < existing.end_line or
                    existing.start_line < item.start_line <= existing.end_line < item.end_line):
                return None
        if not is_nested:
            outermost.append(item)
    return outermost or None


_SUGGESTED_REVIEWER_RE = re.compile(
    r'\bdata-coverage-reviewer\s*=\s*(["\'])(.*?)\1', re.I | re.S
)


def _extract_suggested_reviewer(raw_tag: str) -> str:
    match = _SUGGESTED_REVIEWER_RE.search(raw_tag or "")
    return html.unescape(match.group(2)).strip() if match else ""


def _suggested_reviewer_for_line(reviewers_by_line, line_number: int, fallback: str = "") -> str:
    if reviewers_by_line:
        value = reviewers_by_line.get(line_number)
        if value is None:
            value = reviewers_by_line.get(str(line_number))
        if isinstance(value, dict):
            value = value.get("reviewer") or value.get("author_name") or ""
        if value is not None:
            return str(value or "").strip()
    return str(fallback or "").strip()


def _effective_reviewer_for_line(item: Dict[str, Any], analysis_map: Dict[int, Dict[str, Any]]) -> str:
    """Return the reviewer that the current block will actually display."""
    record = analysis_map.get(item.get("line_no")) or {}
    return str(record.get("reviewer") or item.get("suggested_reviewer") or "").strip()


def parse_source_lines_from_gcov_html(
    content: str,
    project_name: str = "",
    file_path: str = "",
    analysis_records: Optional[List[Dict[str, Any]]] = None,
    review_scope: str = "full",
    incremental_line_numbers: Optional[Set[int]] = None,
    report_id: str = "",
    suggested_reviewers_by_line: Optional[Dict[Any, Any]] = None,
    known_function_ranges: Optional[List[Any]] = None,
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
            suggested_reviewer = _suggested_reviewer_for_line(
                suggested_reviewers_by_line, line_no, _extract_suggested_reviewer(full_tag)
            )

            cov_state = "uncovered" if is_uncov else ("covered" if is_cov else "ignored")

            raw_lines.append({
                "line_no": line_no,
                "code_text": code_text,
                "raw_html": inner_html.strip(),
                "coverage_state": cov_state,
                "is_uncovered": is_uncov,
                "is_incremental": is_inc,
                "suggested_reviewer": suggested_reviewer,
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
            suggested_reviewer = _suggested_reviewer_for_line(
                suggested_reviewers_by_line, line_no, _extract_suggested_reviewer(tail)
            )

            cov_state = "uncovered" if is_uncov else ("covered" if is_cov else "ignored")

            raw_lines.append({
                "line_no": line_no,
                "code_text": code_text,
                "raw_html": tail.strip(),
                "coverage_state": cov_state,
                "is_uncovered": is_uncov,
                "is_incremental": is_inc,
                "suggested_reviewer": suggested_reviewer,
            })
    else:
        # Check if this is stripped HTML or empty page
        if '<pre class="source"></pre>' in content or '<pre class="source"/>' in content or '<meta name="coverage-render-mode"' in content:
            if report_id:
                raise ValueError(f"Cannot parse source lines from stripped HTML report for report_id='{report_id}'")
        elif not report_id:
            # Fallback only for non-lazy plain text files
            plain_lines = content.splitlines()
            for idx, pl in enumerate(plain_lines, start=1):
                raw_lines.append({
                    "line_no": idx,
                    "code_text": pl.strip(),
                    "raw_html": pl,
                    "coverage_state": "ignored",
                    "is_uncovered": False,
                    "is_incremental": False,
                    "suggested_reviewer": _suggested_reviewer_for_line(
                        suggested_reviewers_by_line, idx, ""
                    ),
                })

    # Ensure continuous 1..N
    raw_lines.sort(key=lambda item: item["line_no"])

    # A complete, validated LCOV range is authoritative for this parse.  Any
    # incomplete/invalid/conflicting input deliberately falls back to the
    # existing brace-aware source parser.
    function_ranges = _validated_known_function_ranges(
        known_function_ranges, max((item["line_no"] for item in raw_lines), default=0)
    ) if known_function_ranges is not None else None
    if function_ranges is None:
        function_ranges = extract_c_function_ranges(raw_lines)

    # Attach function_name with one sorted pointer sweep: O(lines + functions).
    sorted_functions = sorted(
        function_ranges, key=lambda fn: (fn.start_line, fn.end_line)
    )
    function_index = 0
    for item in raw_lines:
        line_no = item["line_no"]
        while (
            function_index + 1 < len(sorted_functions)
            and sorted_functions[function_index + 1].start_line <= line_no
        ):
            function_index += 1
        if sorted_functions:
            current_function = sorted_functions[function_index]
            if current_function.start_line <= line_no <= current_function.end_line:
                item["function_name"] = current_function.name

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
            start_reviewer = _effective_reviewer_for_line(item, analysis_map)
            consumed_until = index

            for j in range(index + 1, len(raw_lines)):
                next_item = raw_lines[j]
                if next_item["coverage_state"] == "covered":
                    break

                next_is_uncov = next_item["is_uncovered"]
                if review_scope == "incremental" and not next_item["is_incremental"]:
                    next_is_uncov = False

                if next_is_uncov:
                    if _effective_reviewer_for_line(next_item, analysis_map) != start_reviewer:
                        break
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
        suggested_reviewer = item.get("suggested_reviewer", "")
        reviewer = (rec.get("reviewer") if rec else "") or suggested_reviewer
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
                suggested_reviewer=suggested_reviewer,
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


MAX_BATCH_TOTAL_LINES = 50000


def read_source_ranges(
    source_context: SourceContext,
    ranges: List[Dict[str, int]],
    max_ranges: int = 1000,
    max_total_lines: Optional[int] = MAX_BATCH_TOTAL_LINES,
) -> List[Dict[str, Any]]:
    """
    Read line DTOs for multiple ranges in a single batch.
    Validates range boundaries and total line count limits.
    """
    if not ranges:
        return []
    if len(ranges) > max_ranges:
        raise ValueError(f"Requested ranges count {len(ranges)} exceeds maximum allowed {max_ranges}")

    total_requested_lines = 0
    for r in ranges:
        s_line = int(r.get("start_line", 0))
        e_line = int(r.get("end_line", 0))
        if s_line <= 0 or e_line <= 0 or s_line > e_line:
            raise ValueError(f"Invalid range: [{s_line}..{e_line}]")
        total_requested_lines += (e_line - s_line + 1)

    if max_total_lines is not None and total_requested_lines > max_total_lines:
        raise ValueError(f"Total requested lines ({total_requested_lines}) exceeds maximum batch limit ({max_total_lines})")

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
    """Serialize SourceContext to a server-side sidecar file in .source_cache directory with atomic replace."""
    if not output_dir or not report_id or not file_path_hash:
        raise ValueError("output_dir, report_id, and file_path_hash are required to save sidecar")
    cache_dir = os.path.join(output_dir, ".source_cache", report_id)
    os.makedirs(cache_dir, exist_ok=True)
    sidecar_path = os.path.join(cache_dir, f"{file_path_hash}.source.json")
    tmp_path = f"{sidecar_path}.tmp.{os.getpid()}_{time.time()}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(source_context.to_dict(), f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, sidecar_path)
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
