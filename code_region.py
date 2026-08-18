"""
Code Region Builder module for OneSensor code coverage detail page.
Calculates expanded and collapsed regions based on pending analysis lines
and function boundaries.
"""

from bisect import bisect_right
import logging
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


class FunctionRange:
    """Represents a function line range in a source file."""

    def __init__(self, start_line: int, end_line: int, name: Optional[str] = None):
        self.start_line = int(start_line)
        self.end_line = int(end_line)
        self.name = str(name).strip() if name is not None else None

    def is_valid(self, total_lines: Optional[int] = None) -> bool:
        if self.start_line <= 0 or self.end_line <= 0:
            return False
        if self.start_line > self.end_line:
            return False
        if total_lines is not None and self.start_line > total_lines:
            return False
        return True

    def contains(self, line_number: int) -> bool:
        return self.start_line <= line_number <= self.end_line

    def to_dict(self) -> dict:
        return {
            "start_line": self.start_line,
            "end_line": self.end_line,
            "name": self.name,
        }

    def __repr__(self) -> str:
        return f"FunctionRange({self.start_line}, {self.end_line}, name={self.name!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FunctionRange):
            return False
        return (
            self.start_line == other.start_line
            and self.end_line == other.end_line
            and self.name == other.name
        )


class CodeRegion:
    """Represents a discrete continuous code region in the layout."""

    def __init__(
        self,
        region_id: str,
        start_line: int,
        end_line: int,
        default_state: str,
        kind: str,
        label: Optional[str] = None,
    ):
        self.region_id = str(region_id)
        self.start_line = int(start_line)
        self.end_line = int(end_line)
        self.default_state = str(default_state)  # 'expanded' | 'collapsed'
        self.kind = str(kind)                    # 'analysis' | 'collapsed'
        self.label = str(label) if label is not None else None

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1

    def to_dict(self) -> dict:
        return {
            "region_id": self.region_id,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "default_state": self.default_state,
            "kind": self.kind,
            "label": self.label,
            "line_count": self.line_count,
        }

    def __repr__(self) -> str:
        return (
            f"CodeRegion({self.region_id!r}, {self.start_line}..{self.end_line}, "
            f"state={self.default_state!r}, kind={self.kind!r}, label={self.label!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CodeRegion):
            return False
        return (
            self.region_id == other.region_id
            and self.start_line == other.start_line
            and self.end_line == other.end_line
            and self.default_state == other.default_state
            and self.kind == other.kind
            and self.label == other.label
        )


def sanitize_function_ranges(
    function_ranges: Optional[List[FunctionRange]], total_lines: Optional[int] = None
) -> List[FunctionRange]:
    """Filter out invalid function ranges and sort them by start_line."""
    if not function_ranges:
        return []

    valid_ranges = []
    for fn in function_ranges:
        if not isinstance(fn, FunctionRange):
            if isinstance(fn, dict):
                fn = FunctionRange(
                    start_line=fn.get("start_line", 0),
                    end_line=fn.get("end_line", 0),
                    name=fn.get("name"),
                )
            elif isinstance(fn, (list, tuple)) and len(fn) >= 2:
                fn = FunctionRange(
                    start_line=fn[0],
                    end_line=fn[1],
                    name=fn[2] if len(fn) > 2 else None,
                )
            else:
                logger.warning(f"[CodeRegion] Ignored non-FunctionRange item: {fn}")
                continue

        if fn.is_valid(total_lines):
            clipped_end = min(fn.end_line, total_lines) if total_lines else fn.end_line
            if clipped_end >= fn.start_line:
                valid_ranges.append(
                    FunctionRange(fn.start_line, clipped_end, fn.name)
                )
        else:
            logger.warning(f"[CodeRegion] Ignored invalid function range: {fn}")

    valid_ranges.sort(key=lambda item: (item.start_line, item.end_line))
    return valid_ranges


def find_function_containing_line(
    line_number: int,
    sorted_function_ranges: List[FunctionRange],
    start_lines: Optional[List[int]] = None,
) -> Optional[FunctionRange]:
    """
    Find the function containing line_number in O(log F) time.
    If multiple nested functions contain the line, returns the most specific (innermost/smallest).
    """
    if not sorted_function_ranges:
        return None

    if start_lines is None:
        start_lines = [fn.start_line for fn in sorted_function_ranges]

    idx = bisect_right(start_lines, line_number)
    if idx == 0:
        return None

    cand = sorted_function_ranges[idx - 1]
    if cand.contains(line_number):
        return cand

    for i in range(idx - 2, -1, -1):
        fn = sorted_function_ranges[i]
        if fn.contains(line_number):
            return fn

    return None


def build_code_regions(
    total_lines: int,
    pending_lines: List[int],
    function_ranges: Optional[List[FunctionRange]] = None,
    fallback_context: int = 20,
    max_merge_gap: int = 20,
) -> List[CodeRegion]:
    """
    Build continuous, non-overlapping CodeRegion list covering 1..total_lines.

    Business Rules:
    1. If a pending line maps to a function: expand the entire function.
    2. If a pending line has no function: expand [line - 20, line + 20] (clipped to 1..total_lines).
    3. Overlapping expanded ranges are merged.
    4. Two expanded ranges with gap <= 20 lines (gap = next.start - cur.end - 1) are merged.
    5. Gaps between expanded ranges are filled as collapsed regions.
    6. If whole file has no pending lines, return a single collapsed region [1..total_lines].
    """
    total_lines = int(total_lines)
    if total_lines <= 0:
        return []

    valid_pending = sorted({
        int(line) for line in (pending_lines or [])
        if 1 <= int(line) <= total_lines
    })

    if not valid_pending:
        return [
            CodeRegion(
                region_id=f"region-1-{total_lines}",
                start_line=1,
                end_line=total_lines,
                default_state="collapsed",
                kind="collapsed",
                label=None,
            )
        ]

    valid_functions = sanitize_function_ranges(function_ranges, total_lines)
    start_lines = [fn.start_line for fn in valid_functions]

    # Map pending lines to expanded ranges via fast two-pointer sweep + O(log F) bisect fallback
    raw_expanded_ranges: List[Tuple[int, int, str, Optional[str]]] = []
    fn_idx = 0
    num_fns = len(valid_functions)

    for line in valid_pending:
        while fn_idx + 1 < num_fns and valid_functions[fn_idx + 1].start_line <= line:
            fn_idx += 1

        matched_fn = None
        if num_fns > 0:
            cand = valid_functions[fn_idx]
            if cand.contains(line):
                matched_fn = cand
            else:
                matched_fn = find_function_containing_line(line, valid_functions, start_lines)

        if matched_fn is not None:
            label = matched_fn.name if matched_fn.name else None
            raw_expanded_ranges.append((matched_fn.start_line, matched_fn.end_line, "analysis", label))
        else:
            s_line = max(1, line - fallback_context)
            e_line = min(total_lines, line + fallback_context)
            raw_expanded_ranges.append((s_line, e_line, "analysis", None))

    # Sort raw expanded ranges by start_line
    raw_expanded_ranges.sort(key=lambda item: (item[0], item[1]))

    # Merge overlapping or close (gap <= max_merge_gap) expanded ranges
    merged_expanded: List[Tuple[int, int, str, Optional[str]]] = []
    for cur_start, cur_end, cur_kind, cur_label in raw_expanded_ranges:
        if not merged_expanded:
            merged_expanded.append((cur_start, cur_end, cur_kind, cur_label))
            continue

        prev_start, prev_end, prev_kind, prev_label = merged_expanded[-1]

        # Calculate gap: next_start - cur_end - 1
        gap = cur_start - prev_end - 1
        if gap <= max_merge_gap:
            # Merge
            new_end = max(prev_end, cur_end)
            if prev_label and cur_label and prev_label != cur_label:
                new_label = f"{prev_label}, {cur_label}" if not prev_label.endswith("等") else prev_label
                if len(new_label) > 40:
                    first_fn = prev_label.split(",")[0].strip()
                    new_label = f"{first_fn} 等"
            else:
                new_label = prev_label or cur_label
            merged_expanded[-1] = (prev_start, new_end, "analysis", new_label)
        else:
            merged_expanded.append((cur_start, cur_end, cur_kind, cur_label))

    # Fill collapsed regions between expanded ranges to form continuous 1..total_lines coverage
    final_regions: List[CodeRegion] = []
    current_cursor = 1

    for exp_start, exp_end, exp_kind, exp_label in merged_expanded:
        if exp_start > current_cursor:
            # Collapsed region before current expanded region
            col_start = current_cursor
            col_end = exp_start - 1
            final_regions.append(
                CodeRegion(
                    region_id=f"region-{col_start}-{col_end}",
                    start_line=col_start,
                    end_line=col_end,
                    default_state="collapsed",
                    kind="collapsed",
                    label=None,
                )
            )

        # Add expanded region
        final_regions.append(
            CodeRegion(
                region_id=f"region-{exp_start}-{exp_end}",
                start_line=exp_start,
                end_line=exp_end,
                default_state="expanded",
                kind=exp_kind,
                label=exp_label,
            )
        )
        current_cursor = exp_end + 1

    # Trailing collapsed region if needed
    if current_cursor <= total_lines:
        col_start = current_cursor
        col_end = total_lines
        final_regions.append(
            CodeRegion(
                region_id=f"region-{col_start}-{col_end}",
                start_line=col_start,
                end_line=col_end,
                default_state="collapsed",
                kind="collapsed",
                label=None,
            )
        )

    return final_regions
