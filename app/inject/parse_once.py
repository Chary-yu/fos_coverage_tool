"""
Inject Parse Once Module (Item 10)
Parses .gcov.html source files in a single pass to produce ParsedSourceArtifact:
- Source lines and coverage status
- Function ranges
- Line-index records for DB sync
- SourceContext for sidecar persistence
Eliminates redundant multi-pass HTML parsing across injector workers.
"""

import os
import sys
import hashlib
from typing import Dict, Any, List, Tuple, Optional, Set

from source_reader import (
    SourceContext,
    SourceLineDTO,
    compute_file_path_hash,
    extract_c_function_ranges,
    parse_source_lines_from_gcov_html
)

class ParsedSourceArtifact:
    def __init__(
        self,
        project_name: str,
        report_id: str,
        file_path: str,
        html_content: str,
        review_scope: str = "full",
        incremental_lines: Optional[Set[int]] = None
    ):
        self.project_name = project_name
        self.report_id = report_id
        self.file_path = file_path
        self.file_path_hash = compute_file_path_hash(file_path)
        self.review_scope = review_scope
        
        # Single-pass parse
        self.source_context: SourceContext = parse_source_lines_from_gcov_html(
            content=html_content,
            project_name=project_name,
            file_path=file_path,
            review_scope=review_scope,
            incremental_line_numbers=incremental_lines,
            report_id=report_id
        )
        
        self.source_lines = self.source_context.lines
        self.function_ranges = self.source_context.function_ranges
        
        # Build line-index records in same pass
        self.line_index_records: List[Dict[str, Any]] = self._build_line_index_records()

    def _build_line_index_records(self) -> List[Dict[str, Any]]:
        """Construct DB index records for uncovered lines."""
        records = []
        for line in self.source_lines:
            if line.coverage_state == "uncovered":
                records.append({
                    "project_name": self.project_name,
                    "file_path_hash": self.file_path_hash,
                    "file_path": self.file_path,
                    "line_number": line.line_no,
                    "block_start_line": line.block_start_line or line.line_no,
                    "block_end_line": line.block_end_line or line.line_no,
                    "block_type": line.block_type or "statement",
                    "code_line_hash": hashlib.sha256((line.source or "").strip().encode("utf-8")).hexdigest()[:16],
                    "code_occurrence": 1
                })
        return records

def parse_gcov_source_once(
    project_name: str,
    report_id: str,
    file_path: str,
    html_content: str,
    review_scope: str = "full"
) -> ParsedSourceArtifact:
    """Convenience factory for single-pass artifact generation."""
    return ParsedSourceArtifact(
        project_name=project_name,
        report_id=report_id,
        file_path=file_path,
        html_content=html_content,
        review_scope=review_scope
    )
