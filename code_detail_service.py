"""
Code Detail Service module for OneSensor code coverage detail page.
Provides high-level APIs for layout calculation, batch lines loading,
and single-region source retrieval with security validation and caching.
"""

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from code_region import CodeRegion, FunctionRange, build_code_regions
from source_reader import (
    SourceContext,
    SourceLineDTO,
    parse_source_lines_from_gcov_html,
    read_source_lines,
    read_source_ranges,
)

logger = logging.getLogger(__name__)


def is_safe_relative_path(path: str) -> bool:
    """Validate that path does not attempt path traversal outside permitted root."""
    if not path or not isinstance(path, str):
        return False
    normalized = path.replace("\\", "/").strip()
    if normalized.startswith("/") or normalized.startswith("..") or "/../" in normalized:
        return False
    if re.match(r'^[A-Za-z]:', normalized) or ":" in normalized:
        return False
    return True


class CodeDetailService:
    """Service handling code detail layout calculation and range retrieval."""

    def __init__(
        self,
        db_manager=None,
        search_dirs: Optional[List[str]] = None,
        review_scope: str = "full",
    ):
        self.db_manager = db_manager
        self.search_dirs = [os.path.abspath(d) for d in (search_dirs or []) if os.path.isdir(d)]
        self.review_scope = review_scope
        self._context_cache: Dict[Tuple[str, str], Tuple[float, SourceContext]] = {}
        self._cache_ttl_sec = 60.0

    def add_search_dir(self, directory: str):
        if directory and os.path.isdir(directory):
            abs_dir = os.path.abspath(directory)
            if abs_dir not in self.search_dirs:
                self.search_dirs.append(abs_dir)

    def locate_gcov_file(self, file_path: str) -> Optional[str]:
        """Locate the .gcov.html file corresponding to file_path across search_dirs."""
        clean_path = file_path.replace("\\", "/").strip().lstrip("/")
        base_name = os.path.basename(clean_path)

        candidates = [
            clean_path + ".gcov.html",
            clean_path if clean_path.endswith(".gcov.html") else "",
            base_name + ".gcov.html",
            base_name if base_name.endswith(".gcov.html") else "",
        ]
        candidates = [c for c in candidates if c]

        for s_dir in self.search_dirs:
            for cand in candidates:
                full_path = os.path.join(s_dir, cand)
                if os.path.isfile(full_path):
                    return full_path

            # Recursive search in search_dirs if not directly found
            for root, _, files in os.walk(s_dir):
                for f in files:
                    if f.endswith(".gcov.html") and (f in candidates or f == base_name + ".gcov.html"):
                        return os.path.join(root, f)

        return None

    def get_source_context(
        self,
        project_name: str,
        file_path: str,
        content_override: Optional[str] = None,
    ) -> SourceContext:
        """Resolve or load SourceContext for the given project and file."""
        if not project_name or not file_path:
            raise ValueError("project_name and file_path are required")

        if not is_safe_relative_path(file_path):
            raise ValueError(f"Illegal or unsafe file path: {file_path}")

        cache_key = (project_name, file_path)
        now = time.time()
        if not content_override and cache_key in self._context_cache:
            ts, cached_ctx = self._context_cache[cache_key]
            if now - ts < self._cache_ttl_sec:
                # Refresh DB analysis state in cached context if DB is available
                if self.db_manager:
                    self._refresh_analysis_records(cached_ctx, project_name, file_path)
                return cached_ctx

        # Fetch analysis records from DB
        analysis_records = []
        if self.db_manager and hasattr(self.db_manager, "fetch_records"):
            try:
                analysis_records = self.db_manager.fetch_records(project_name, file_path) or []
            except Exception as e:
                logger.warning(f"[CodeDetailService] Failed to fetch DB records for {file_path}: {e}")

        # If content is provided directly (e.g. in tests or injected page)
        if content_override:
            context = parse_source_lines_from_gcov_html(
                content=content_override,
                project_name=project_name,
                file_path=file_path,
                analysis_records=analysis_records,
                review_scope=self.review_scope,
            )
            self._context_cache[cache_key] = (now, context)
            return context

        # Try to locate .gcov.html on disk
        gcov_file = self.locate_gcov_file(file_path)
        if gcov_file and os.path.isfile(gcov_file):
            try:
                with open(gcov_file, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                context = parse_source_lines_from_gcov_html(
                    content=content,
                    project_name=project_name,
                    file_path=file_path,
                    analysis_records=analysis_records,
                    review_scope=self.review_scope,
                )
                self._context_cache[cache_key] = (now, context)
                return context
            except Exception as e:
                logger.warning(f"[CodeDetailService] Error reading file {gcov_file}: {e}")

        # Fallback: synthesize context from DB line index if available
        if self.db_manager and hasattr(self.db_manager, "fetch_full_detail_page"):
            try:
                detail_page = self.db_manager.fetch_full_detail_page(project_name, file_path, page=1, page_size=10000)
                if detail_page and detail_page.get("rows"):
                    context = self._synthesize_context_from_db(project_name, file_path, detail_page, analysis_records)
                    self._context_cache[cache_key] = (now, context)
                    return context
            except Exception as e:
                logger.warning(f"[CodeDetailService] Error synthesizing from DB: {e}")

        # If file not found anywhere, create empty context
        context = SourceContext(
            project_name=project_name,
            file_path=file_path,
            lines=[],
            function_ranges=[],
            pending_lines=[],
        )
        self._context_cache[cache_key] = (now, context)
        return context

    def _refresh_analysis_records(self, context: SourceContext, project_name: str, file_path: str):
        """Update review state on existing lines from database."""
        try:
            records = self.db_manager.fetch_records(project_name, file_path) or []
            rec_map = {int(r["line_number"]): r for r in records if "line_number" in r}
            pending_lines = []
            for line in context.lines:
                rec = rec_map.get(line.line_no)
                if rec:
                    line.analysis_state = rec.get("status") or "未确认"
                    line.reviewer = rec.get("reviewer") or ""
                    line.is_draft = bool(rec.get("is_draft", False))
                    line.coverage_method = rec.get("coverage_method") or ""
                    line.uncovered_reason = rec.get("uncovered_reason") or ""

                # Re-evaluate pending
                if line.coverage_state == "uncovered":
                    if line.analysis_state not in ("可覆盖", "无法覆盖", "冗余代码") or line.is_draft:
                        line.is_pending_analysis = True
                        pending_lines.append(line.line_no)
                    else:
                        line.is_pending_analysis = False
                else:
                    line.is_pending_analysis = False
            context.pending_lines = pending_lines
        except Exception as e:
            logger.debug(f"[CodeDetailService] refresh records failed: {e}")

    def _synthesize_context_from_db(
        self,
        project_name: str,
        file_path: str,
        detail_page: dict,
        analysis_records: List[dict],
    ) -> SourceContext:
        """Construct a SourceContext from DB coverage_line_index rows."""
        headers = detail_page.get("headers", [])
        rows = detail_page.get("rows", [])
        lines = []
        pending_lines = []
        functions = []

        fn_map = {}
        for row in rows:
            row_dict = dict(zip(headers, row))
            line_no = int(row_dict.get("line_number", 0))
            if line_no <= 0:
                continue

            status = row_dict.get("status") or "未确认"
            fill_status = row_dict.get("fill_status") or "未填写"
            is_pending = (fill_status == "未填写" or status not in ("可覆盖", "无法覆盖", "冗余代码"))
            if is_pending:
                pending_lines.append(line_no)

            fn_name = row_dict.get("function_name") or ""
            if fn_name:
                if fn_name not in fn_map:
                    fn_map[fn_name] = [line_no, line_no]
                else:
                    fn_map[fn_name][1] = max(fn_map[fn_name][1], line_no)

            lines.append(
                SourceLineDTO(
                    line_no=line_no,
                    source=row_dict.get("line_text") or "",
                    raw_html=html.escape(row_dict.get("line_text") or ""),
                    coverage_state="uncovered",
                    analysis_state=status,
                    is_pending_analysis=is_pending,
                    reviewer=row_dict.get("reviewer") or "",
                    coverage_method=row_dict.get("coverage_method") or "",
                    uncovered_reason=row_dict.get("uncovered_reason") or "",
                    is_draft=False,
                    block_start_line=int(row_dict.get("block_start_line") or line_no),
                    block_end_line=int(row_dict.get("block_end_line") or line_no),
                    block_type=row_dict.get("block_type") or "single",
                    function_name=fn_name,
                    is_block_entry=int(row_dict.get("block_start_line") or line_no) == line_no,
                )
            )

        for fname, (s_l, e_l) in fn_map.items():
            functions.append(FunctionRange(s_l, e_l, fname))

        return SourceContext(
            project_name=project_name,
            file_path=file_path,
            lines=lines,
            function_ranges=functions,
            pending_lines=pending_lines,
        )

    def get_code_layout(
        self,
        project_name: str,
        file_path: str,
        content_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compute the CodeRegion layout for the file."""
        t_start = time.perf_counter()
        context = self.get_source_context(project_name, file_path, content_override=content_override)

        regions = build_code_regions(
            total_lines=context.total_lines,
            pending_lines=context.pending_lines,
            function_ranges=context.function_ranges,
        )
        elapsed_ms = (time.perf_counter() - t_start) * 1000.0

        expanded_count = sum(1 for r in regions if r.default_state == "expanded")
        expanded_lines = sum(r.line_count for r in regions if r.default_state == "expanded")
        collapsed_lines = sum(r.line_count for r in regions if r.default_state == "collapsed")

        perf_stats = {
            "layout_build_ms": round(elapsed_ms, 2),
            "pending_line_count": len(context.pending_lines),
            "region_count": len(regions),
            "expanded_region_count": expanded_count,
            "expanded_line_count": expanded_lines,
            "collapsed_line_count": collapsed_lines,
        }
        logger.info(f"[CodeDetailService] Layout built for {file_path}: {perf_stats}")

        return {
            "project_name": project_name,
            "file_path": file_path,
            "total_lines": context.total_lines,
            "pending_line_count": len(context.pending_lines),
            "regions": [r.to_dict() for r in regions],
            "perf": perf_stats,
        }

    def get_code_lines_batch(
        self,
        project_name: str,
        file_path: str,
        ranges: List[Dict[str, int]],
        content_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Batch load line data for specified ranges."""
        t_start = time.perf_counter()
        context = self.get_source_context(project_name, file_path, content_override=content_override)
        range_results = read_source_ranges(context, ranges)
        elapsed_ms = (time.perf_counter() - t_start) * 1000.0

        total_lines_read = sum(len(r["lines"]) for r in range_results)
        logger.info(
            f"[CodeDetailService] Batch lines read for {file_path}: "
            f"ranges={len(ranges)}, lines={total_lines_read}, elapsed={round(elapsed_ms, 2)}ms"
        )
        return {
            "project_name": project_name,
            "file_path": file_path,
            "ranges": range_results,
            "perf": {
                "batch_load_ms": round(elapsed_ms, 2),
                "total_lines_read": total_lines_read,
            },
        }

    def get_code_lines_single(
        self,
        project_name: str,
        file_path: str,
        start_line: int,
        end_line: int,
        content_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Load single range line data."""
        t_start = time.perf_counter()
        context = self.get_source_context(project_name, file_path, content_override=content_override)
        lines = read_source_lines(context, start_line, end_line)
        elapsed_ms = (time.perf_counter() - t_start) * 1000.0

        return {
            "project_name": project_name,
            "file_path": file_path,
            "start_line": int(start_line),
            "end_line": int(end_line),
            "lines": lines,
            "perf": {
                "load_ms": round(elapsed_ms, 2),
                "line_count": len(lines),
            },
        }
