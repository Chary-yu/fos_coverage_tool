"""
Inject Parse Once Module (Item 10)
Parses .gcov.html source files in a single pass to produce ParsedSourceArtifact:
- Source lines and coverage status
- Function ranges
- Full line-index records adhering to the complete database contract
- SourceContext for sidecar persistence
"""

import os
import sys
import hashlib
from typing import Dict, Any, List, Tuple, Optional, Set

from source_reader import (
    SourceContext,
    SourceLineDTO,
    calc_sidecar_file_key,
    compute_db_file_path_hash,
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
        # Keep the two identity contracts explicit: DB rows use the historical
        # MD5 key, while sidecar directories use the SHA256-derived key.
        self.db_file_path_hash = compute_db_file_path_hash(file_path)
        self.sidecar_file_key = calc_sidecar_file_key(file_path)
        self.file_path_hash = self.db_file_path_hash  # compatibility field
        self.source_file_name = os.path.basename(file_path)
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
        
        # Build line-index records adhering to full DB contract
        self.line_index_records: List[Dict[str, Any]] = self._build_line_index_records()

    def _build_line_index_records(self) -> List[Dict[str, Any]]:
        """Construct full DB index records for uncovered lines."""
        records = []
        code_occurrence_tracker: Dict[str, int] = {}

        for line in self.source_lines:
            if line.coverage_state == "uncovered":
                line_str = (line.source or "").strip()
                code_hash = hashlib.sha256(line_str.encode("utf-8")).hexdigest()[:16]
                
                # Occurrence index for identical lines in file
                occ = code_occurrence_tracker.get(code_hash, 0) + 1
                code_occurrence_tracker[code_hash] = occ
                
                func_name = line.function_name or ""
                func_hash = hashlib.sha256(func_name.strip().encode("utf-8")).hexdigest()[:16] if func_name else ""
                
                records.append({
                    "project_name": self.project_name,
                    "file_path_hash": self.file_path_hash,
                    "file_path": self.file_path,
                    "source_file_name": self.source_file_name,
                    "line_number": line.line_no,
                    "line_text": line.source or "",
                    "block_start_line": line.block_start_line or line.line_no,
                    "block_end_line": line.block_end_line or line.line_no,
                    "block_type": line.block_type or "statement",
                    "function_name": func_name,
                    "function_hash": func_hash,
                    "code_line_hash": code_hash,
                    "code_occurrence": occ
                })
        return records

def parse_gcov_source_once(
    project_name: str,
    report_id: str,
    file_path: str,
    html_content: str,
    review_scope: str = "full",
    incremental_lines: Optional[Set[int]] = None
) -> ParsedSourceArtifact:
    """Convenience factory for single-pass artifact generation."""
    return ParsedSourceArtifact(
        project_name=project_name,
        report_id=report_id,
        file_path=file_path,
        html_content=html_content,
        review_scope=review_scope,
        incremental_lines=incremental_lines
    )
