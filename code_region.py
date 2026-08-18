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
    line_number: int, sorted_function_ranges: List[FunctionRange]
) -> Optional[FunctionRange]:
    """
    Find the function containing line_number in O(log F) time.
    If multiple nested functions contain the line, returns the most specific (innermost/smallest).
    """
    if not sorted_function_ranges:
        return None

    start_lines = [fn.start_line for fn in sorted_function_ranges]
    idx = bisect_right(start_lines, line_number)

    candidates = []
    for i in range(idx - 1, -1, -1):
        fn = sorted_function_ranges[i]
        if fn.contains(line_number):
            candidates.append(fn)

    if not candidates:
        return None

    candidates.sort(key=lambda fn: (fn.end_line - fn.start_line, -fn.start_line))
    return candidates[0]


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

    sorted_functions = sanitize_function_ranges(function_ranges, total_lines=total_lines)

    raw_expanded: List[Tuple[int, int, Optional[str]]] = []
    for line in valid_pending:
        fn = find_function_containing_line(line, sorted_functions)
        if fn:
            raw_expanded.append((fn.start_line, fn.end_line, fn.name))
        else:
            start_l = max(1, line - fallback_context)
            end_l = min(total_lines, line + fallback_context)
            raw_expanded.append((start_l, end_l, None))

    raw_expanded.sort(key=lambda item: (item[0], item[1]))

    merged_expanded: List[Tuple[int, int, Optional[str]]] = []
    for cur_start, cur_end, cur_label in raw_expanded:
        if not merged_expanded:
            merged_expanded.append((cur_start, cur_end, cur_label))
            continue

        prev_start, prev_end, prev_label = merged_expanded[-1]

        # gap between prev_end and cur_start:
        # e.g. prev_end=180, cur_start=201 -> gap = 201 - 180 - 1 = 20
        gap = cur_start - prev_end - 1

        if gap <= max_merge_gap:
            new_end = max(prev_end, cur_end)
            labels = []
            if prev_label:
                labels.append(prev_label)
            if cur_label and cur_label not in labels:
                labels.append(cur_label)
            new_label = ", ".join(labels) if labels else None

            merged_expanded[-1] = (prev_start, new_end, new_label)
        else:
            merged_expanded.append((cur_start, cur_end, cur_label))

    regions: List[CodeRegion] = []
    cursor = 1

    for start_l, end_l, label in merged_expanded:
        start_l = max(1, start_l)
        end_l = min(total_lines, end_l)

        if start_l > cursor:
            collapsed_start = cursor
            collapsed_end = start_l - 1
            regions.append(
                CodeRegion(
                    region_id=f"region-{collapsed_start}-{collapsed_end}",
                    start_line=collapsed_start,
                    end_line=collapsed_end,
                    default_state="collapsed",
                    kind="collapsed",
                    label=None,
                )
            )

        regions.append(
            CodeRegion(
                region_id=f"region-{start_l}-{end_l}",
                start_line=start_l,
                end_line=end_l,
                default_state="expanded",
                kind="analysis",
                label=label,
            )
        )
        cursor = end_l + 1

    if cursor <= total_lines:
        regions.append(
            CodeRegion(
                region_id=f"region-{cursor}-{total_lines}",
                start_line=cursor,
                end_line=total_lines,
                default_state="collapsed",
                kind="collapsed",
                label=None,
            )
        )

    return regions
