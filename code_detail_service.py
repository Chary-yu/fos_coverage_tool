"""
Code Detail Service module for code coverage detail page.
Provides high-level APIs for layout calculation, batch lines loading,
and single-region source retrieval with security validation, sidecar caching,
and exact report ID binding.
"""

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

from code_region import CodeRegion, FunctionRange, build_code_regions
from source_reader import (
    SourceContext,
    SourceLineDTO,
    calc_sidecar_file_key,
    compute_file_path_hash,
    is_line_pending_analysis,
    is_valid_report_id,
    is_valid_review_scope,
    load_source_sidecar,
    parse_source_lines_from_gcov_html,
    read_source_lines,
    read_source_ranges,
)

logger = logging.getLogger(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_REGISTRY_DIR = os.environ.get(
    "COVERAGE_REGISTRY_DIR",
    "/var/lib/onesensor-coverage/report-registry"
    if os.path.exists("/var/lib/onesensor-coverage/report-registry") or (os.path.exists("/var/lib") and os.access("/var/lib", os.W_OK))
    else os.path.join(tempfile.gettempdir(), ".onesensor_report_registry")
)
REPORT_REGISTRY_LEGACY_PATH = os.path.join(SCRIPT_DIR, ".report_registry.json")


def load_report_registry() -> Dict[str, List[str]]:
    """Load persistent report registry from per-report files and legacy registry."""
    result: Dict[str, List[str]] = {}

    if os.path.isdir(REPORT_REGISTRY_DIR):
        try:
            for entry in os.listdir(REPORT_REGISTRY_DIR):
                if entry.endswith(".json") and not entry.startswith("."):
                    file_path = os.path.join(REPORT_REGISTRY_DIR, entry)
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        if isinstance(data, dict):
                            r_id = data.get("report_id") or os.path.splitext(entry)[0]
                            dirs = data.get("directories", [])
                            if isinstance(dirs, str):
                                dirs = [dirs]
                            result[r_id] = [str(d) for d in dirs if d]
                    except Exception:
                        pass
        except Exception:
            pass

    if os.path.isfile(REPORT_REGISTRY_LEGACY_PATH):
        try:
            with open(REPORT_REGISTRY_LEGACY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in data.items():
                    if k not in result:
                        if isinstance(v, str):
                            result[k] = [v]
                        elif isinstance(v, list):
                            result[k] = [str(x) for x in v if x]
        except Exception:
            pass

    return result


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
        self._context_cache: Dict[Tuple[str, str, str, str], Tuple[float, SourceContext]] = {}
        self._cache_ttl_sec = 60.0

    def add_search_dir(self, directory: str):
        if directory and os.path.isdir(directory):
            abs_dir = os.path.abspath(directory)
            if abs_dir not in self.search_dirs:
                self.search_dirs.append(abs_dir)

    def locate_gcov_file(self, file_path: str, report_id: str = "") -> Optional[str]:
        """
        Locate the exact .gcov.html file for file_path and report_id.
        Avoids ambiguous recursive guessing across unrelated reports.
        """
        clean_path = file_path.replace("\\", "/").strip().lstrip("/")
        candidates = [
            clean_path + ".gcov.html",
            clean_path if clean_path.endswith(".gcov.html") else "",
        ]
        candidates = [c for c in candidates if c]

        dirs_to_check = list(self.search_dirs)
        if report_id:
            registry = load_report_registry()
            if report_id in registry:
                for r_dir in registry[report_id]:
                    if r_dir not in dirs_to_check:
                        dirs_to_check.insert(0, r_dir)

        # 1. If report_id specified, check subdirectories matching report_id
        if report_id:
            for s_dir in dirs_to_check:
                for cand in candidates:
                    target = os.path.join(s_dir, report_id, cand)
                    if os.path.isfile(target):
                        return target

        # 2. Check exact paths relative to search_dirs
        for s_dir in dirs_to_check:
            for cand in candidates:
                target = os.path.join(s_dir, cand)
                if os.path.isfile(target):
                    return target

        return None

    def get_source_context(
        self,
        project_name: str,
        file_path: str,
        report_id: str = "",
        review_scope: str = "full",
        content_override: Optional[str] = None,
    ) -> SourceContext:
        """
        Resolve or load SourceContext for the given project, file, report_id, and review scope.
        Throws FileNotFoundError if the source is not found anywhere.
        """
        if not project_name or not file_path:
            raise ValueError("project_name and file_path are required")

        if not is_safe_relative_path(file_path):
            raise ValueError(f"Illegal or unsafe file path: {file_path}")

        if report_id and not is_valid_report_id(report_id):
            raise ValueError(f"Invalid report_id format: '{report_id}'")

        scope = review_scope or self.review_scope
        if not is_valid_review_scope(scope):
            raise ValueError(f"Invalid review_scope: '{scope}'")

        cache_key = (project_name, report_id or "", file_path, scope)
        now = time.time()

        if not content_override and cache_key in self._context_cache:
            ts, cached_ctx = self._context_cache[cache_key]
            if now - ts < self._cache_ttl_sec:
                if self.db_manager:
                    self._refresh_analysis_records(cached_ctx, project_name, file_path, scope)
                return cached_ctx

        # Fetch analysis records from DB if available
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
                review_scope=scope,
                report_id=report_id,
            )
            self._context_cache[cache_key] = (now, context)
            return context

        file_hash = compute_file_path_hash(file_path)

        # 1. Try to load from server-side source sidecar if report_id given
        if report_id:
            dirs_to_check = list(self.search_dirs)
            registry = load_report_registry()
            if report_id in registry:
                for r_dir in registry[report_id]:
                    if r_dir not in dirs_to_check:
                        dirs_to_check.insert(0, r_dir)
            for std_dir in ["/opt/coverage_reports/review", "/opt/coverage_reports/review_incremental", "/opt/coverage_reports"]:
                if os.path.isdir(std_dir) and std_dir not in dirs_to_check:
                    dirs_to_check.append(std_dir)

            for s_dir in dirs_to_check:
                sidecar_ctx = load_source_sidecar(s_dir, report_id, file_hash)
                if sidecar_ctx:
                    if self.db_manager:
                        self._refresh_analysis_records(sidecar_ctx, project_name, file_path, scope)
                    self._context_cache[cache_key] = (now, sidecar_ctx)
                    return sidecar_ctx

            # Authoritative: when report_id is provided, do NOT fallback to stripped .gcov.html
            raise FileNotFoundError(
                f"Source context sidecar missing or unavailable for project='{project_name}', file='{file_path}', report_id='{report_id}'"
            )

        # 2. Try to locate exact .gcov.html on disk (legacy / non-lazy fallback)
        gcov_file = self.locate_gcov_file(file_path, report_id=report_id)
        if gcov_file and os.path.isfile(gcov_file):
            try:
                with open(gcov_file, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                context = parse_source_lines_from_gcov_html(
                    content=content,
                    project_name=project_name,
                    file_path=file_path,
                    analysis_records=analysis_records,
                    review_scope=scope,
                    report_id=report_id,
                )
                self._context_cache[cache_key] = (now, context)
                return context
            except Exception as e:
                logger.warning(f"[CodeDetailService] Error reading file {gcov_file}: {e}")

        # If source is missing, raise explicit FileNotFoundError
        raise FileNotFoundError(
            f"Source file not found for project='{project_name}', file='{file_path}', report_id='{report_id}'"
        )

    def _refresh_analysis_records(
        self, context: SourceContext, project_name: str, file_path: str, review_scope: str = "full"
    ):
        """Update review state on existing lines from database using unified pending logic."""
        try:
            records = self.db_manager.fetch_records(project_name, file_path) or []
            rec_map = {int(r["line_number"]): r for r in records if "line_number" in r}
            pending_lines = []
            confirmed_count = 0

            for line in context.lines:
                rec = rec_map.get(line.line_no)
                if rec:
                    line.analysis_state = rec.get("status") or "未确认"
                    line.reviewer = rec.get("reviewer") or ""
                    line.is_draft = bool(rec.get("is_draft", False))
                    line.coverage_method = rec.get("coverage_method") or ""
                    line.uncovered_reason = rec.get("uncovered_reason") or ""
                else:
                    line.analysis_state = "未确认"
                    line.reviewer = ""
                    line.is_draft = False
                    line.coverage_method = ""
                    line.uncovered_reason = ""

                is_pending = is_line_pending_analysis(
                    coverage_state=line.coverage_state,
                    analysis_state=line.analysis_state,
                    is_draft=line.is_draft,
                    fill_status="已填写" if rec and rec.get("status") else "未填写",
                )
                line.is_pending_analysis = is_pending

                if is_pending:
                    pending_lines.append(line.line_no)
                elif line.coverage_state == "uncovered" and line.analysis_state in {"可覆盖", "无法覆盖", "冗余代码"} and not line.is_draft:
                    confirmed_count += 1

            context.pending_lines = pending_lines
            context.confirmed_count = confirmed_count
        except Exception as e:
            logger.debug(f"[CodeDetailService] refresh records failed: {e}")

    def get_code_layout(
        self,
        project_name: str,
        file_path: str,
        report_id: str = "",
        review_scope: str = "full",
        content_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compute the CodeRegion layout for the file."""
        t_start = time.perf_counter()
        context = self.get_source_context(
            project_name=project_name,
            file_path=file_path,
            report_id=report_id,
            review_scope=review_scope,
            content_override=content_override,
        )

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

        return {
            "project_name": project_name,
            "file_path": file_path,
            "report_id": context.report_id,
            "total_lines": context.total_lines,
            "total_uncovered_count": context.total_uncovered_count,
            "pending_line_count": len(context.pending_lines),
            "confirmed_count": context.confirmed_count,
            "regions": [r.to_dict() for r in regions],
            "perf": perf_stats,
        }

    def get_code_lines_batch(
        self,
        project_name: str,
        file_path: str,
        ranges: Optional[List[Dict[str, int]]] = None,
        region_ids: Optional[List[str]] = None,
        report_id: str = "",
        review_scope: str = "full",
        content_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Batch load line data for specified ranges (up to 1000 ranges) or verified default region IDs."""
        t_start = time.perf_counter()
        context = self.get_source_context(
            project_name=project_name,
            file_path=file_path,
            report_id=report_id,
            review_scope=review_scope,
            content_override=content_override,
        )

        verified_ranges = []
        is_verified_default_batch = False

        if region_ids and isinstance(region_ids, list):
            regions = build_code_regions(
                total_lines=context.total_lines,
                pending_lines=context.pending_lines,
                function_ranges=context.function_ranges,
            )
            expanded_region_map = {r.region_id: r for r in regions if r.default_state == "expanded"}
            for reg_id in region_ids:
                if reg_id in expanded_region_map:
                    reg = expanded_region_map[reg_id]
                    verified_ranges.append({"start_line": reg.start_line, "end_line": reg.end_line})
                else:
                    raise ValueError(f"Requested region '{reg_id}' is not a valid default expanded region")
            is_verified_default_batch = True

        if is_verified_default_batch:
            target_ranges = verified_ranges
            max_lines = None
        else:
            if not ranges:
                raise ValueError("ranges or region_ids is required")
            target_ranges = ranges
            max_lines = 50000

        range_results = read_source_ranges(context, target_ranges, max_ranges=1000, max_total_lines=max_lines)
        elapsed_ms = (time.perf_counter() - t_start) * 1000.0

        total_lines_read = sum(len(r["lines"]) for r in range_results)
        return {
            "project_name": project_name,
            "file_path": file_path,
            "report_id": context.report_id,
            "ranges": range_results,
            "perf": {
                "batch_load_ms": round(elapsed_ms, 2),
                "total_lines_read": total_lines_read,
                "verified_default_batch": is_verified_default_batch,
            },
        }

    def get_code_lines_single(
        self,
        project_name: str,
        file_path: str,
        start_line: int,
        end_line: int,
        report_id: str = "",
        review_scope: str = "full",
        content_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Load single range line data."""
        t_start = time.perf_counter()
        context = self.get_source_context(
            project_name=project_name,
            file_path=file_path,
            report_id=report_id,
            review_scope=review_scope,
            content_override=content_override,
        )
        lines = read_source_lines(context, start_line, end_line)
        elapsed_ms = (time.perf_counter() - t_start) * 1000.0

        return {
            "project_name": project_name,
            "file_path": file_path,
            "report_id": context.report_id,
            "start_line": int(start_line),
            "end_line": int(end_line),
            "lines": lines,
            "perf": {
                "load_ms": round(elapsed_ms, 2),
                "line_count": len(lines),
            },
        }
