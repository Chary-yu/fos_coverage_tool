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

from .code_region import CodeRegion, FunctionRange, build_code_regions
from app.code_detail.overlay_cache import AnalysisOverlay, AnalysisOverlayCache
from app.reports.registry import ReportRegistry
from .source_reader import (
    SourceContext,
    SourceLineDTO,
    calc_sidecar_file_key,
    compute_db_file_path_hash,
    is_line_pending_analysis,
    is_valid_report_id,
    is_valid_review_scope,
    load_source_sidecar,
    parse_source_lines_from_gcov_html,
    read_source_lines,
    read_source_ranges,
)

# Public compatibility name: this is the historical DB MD5 identity, never
# the SHA-256 sidecar key.
compute_file_path_hash = compute_db_file_path_hash

logger = logging.getLogger(__name__)

SCRIPT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))


def get_configured_registry_dir() -> str:
    if "COVERAGE_REGISTRY_DIR" in os.environ:
        return os.environ["COVERAGE_REGISTRY_DIR"]
    try:
        cfg_path = os.environ.get("COVERAGE_CONFIG_PATH") or os.path.join(SCRIPT_DIR, "coverage_config.json")
        if os.path.isfile(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if cfg.get("report_registry_dir"):
                return cfg["report_registry_dir"]
            state = cfg.get("runtime_state") or {}
            if state.get("root"):
                root = state.get("root")
                if not os.path.isabs(root):
                    root = os.path.join(SCRIPT_DIR, root)
                registry_dir = state.get("registry_dir", "report-registry")
                return os.path.realpath(os.path.join(root, registry_dir))
    except Exception:
        pass
    if os.path.exists("/var/lib/onesensor-coverage/report-registry") or (os.path.exists("/var/lib") and os.access("/var/lib", os.W_OK)):
        return "/var/lib/onesensor-coverage/report-registry"
    return os.path.join(tempfile.gettempdir(), ".onesensor_report_registry")


REPORT_REGISTRY_DIR = get_configured_registry_dir()
REPORT_REGISTRY_LEGACY_PATH = os.path.join(SCRIPT_DIR, ".report_registry.json")


def _report_registry():
    return ReportRegistry(
        get_configured_registry_dir(), legacy_path=REPORT_REGISTRY_LEGACY_PATH
    )


def load_report_registry(report_id: Optional[str] = None) -> Dict[str, List[str]]:
    """Compatibility view backed by the canonical ReportRegistry."""
    registry = _report_registry()
    values = {}
    if report_id:
        item = registry.load_exact(report_id)
        if item:
            values[report_id] = list(item.get("directories") or [])
        return values
    for key, item in registry.load_all().items():
        values[key] = list(item.get("directories") or [])
    return values


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


MAX_CONTEXT_CACHE_ENTRIES = 16
MAX_CONTEXT_CACHE_TOTAL_LINES = 1_000_000


from app.code_detail.sidecar_store import SidecarStore

class CodeDetailService:
    """Service handling code detail layout calculation and range retrieval."""

    def __init__(
        self,
        db_manager=None,
        search_dirs: Optional[List[str]] = None,
        review_scope: str = "full",
        max_cache_entries: int = MAX_CONTEXT_CACHE_ENTRIES,
        max_cache_total_lines: int = MAX_CONTEXT_CACHE_TOTAL_LINES,
    ):
        self.db_manager = db_manager
        all_dirs = list(search_dirs or [])
        env_roots = os.environ.get("COVERAGE_REPORT_ROOTS", "")
        if env_roots:
            split_pat = r'[;,]' if os.name == 'nt' else r'[:,]'
            for r_root in re.split(split_pat, env_roots):
                r_root = r_root.strip()
                if r_root and os.path.exists(r_root):
                    all_dirs.append(r_root)

        self.search_dirs = [os.path.abspath(d) for d in all_dirs if os.path.isdir(d)]
        self.review_scope = review_scope
        self._context_cache: Dict[Tuple[str, str, str, str], Tuple[float, SourceContext]] = {}
        self._overlay_cache = AnalysisOverlayCache()
        self._sidecar_store = SidecarStore(search_dirs=self.search_dirs)
        self._cache_ttl_sec = 60.0
        self._max_cache_entries = max_cache_entries
        self._max_cache_total_lines = max_cache_total_lines

    def _prune_context_cache(self, now: Optional[float] = None):
        """Prune expired cache entries and enforce LRU / max entries & total lines limits."""
        if now is None:
            now = time.time()
        # 1. Remove expired entries
        expired_keys = [
            k for k, (ts, _) in self._context_cache.items()
            if (now - ts) >= self._cache_ttl_sec
        ]
        for k in expired_keys:
            self._context_cache.pop(k, None)

        # 2. Enforce maximum capacity (entries count and total lines sum) by evicting oldest entries
        total_lines_sum = sum(ctx.total_lines for _, ctx in self._context_cache.values())
        if len(self._context_cache) > self._max_cache_entries or total_lines_sum > self._max_cache_total_lines:
            sorted_items = sorted(self._context_cache.items(), key=lambda item: item[1][0])
            for k, (ts, ctx) in sorted_items:
                if len(self._context_cache) <= self._max_cache_entries and total_lines_sum <= self._max_cache_total_lines:
                    break
                self._context_cache.pop(k, None)
                total_lines_sum -= ctx.total_lines

    def clear_context_cache(self):
        """Explicitly clear in-memory context cache."""
        self._context_cache.clear()

    def add_search_dir(self, directory: str):
        if directory and os.path.isdir(directory):
            abs_dir = os.path.abspath(directory)
            if abs_dir not in self.search_dirs:
                self.search_dirs.append(abs_dir)
                self._sidecar_store.add_search_dir(abs_dir)

    def get_analysis_overlay(
        self,
        project_name: str,
        file_path_hash: str,
        file_path: str,
        review_scope: str = "full"
    ) -> Optional[AnalysisOverlay]:
        if not self.db_manager:
            return None
        data_version = 1
        if hasattr(self.db_manager, "get_project_data_version"):
            try:
                data_version = self.db_manager.get_project_data_version(project_name)
            except Exception:
                data_version = 1

        cached = self._overlay_cache.get(project_name, file_path_hash, review_scope, data_version)
        if cached is not None:
            return cached

        try:
            records = self.db_manager.fetch_records(project_name, file_path) or []
        except Exception as e:
            logger.warning(f"[CodeDetailService] fetch_records failed: {e}")
            records = []

        overlay = AnalysisOverlay(
            project_name=project_name,
            file_path_hash=file_path_hash,
            review_scope=review_scope,
            data_version=data_version,
            records=records
        )
        self._overlay_cache.put(overlay)
        return overlay

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
            registry = load_report_registry(report_id)
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
        self._prune_context_cache(now)

        if not content_override and cache_key in self._context_cache:
            ts, cached_ctx = self._context_cache[cache_key]
            if now - ts < self._cache_ttl_sec:
                # Update access timestamp for LRU
                self._context_cache[cache_key] = (now, cached_ctx)
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
            self._prune_context_cache(now)
            return context

        db_file_hash = compute_db_file_path_hash(file_path)
        sidecar_file_hash = calc_sidecar_file_key(file_path)

        # 1. Try to load from server-side source sidecar if report_id given
        if report_id:
            dirs_to_check = list(self.search_dirs)
            registry = load_report_registry(report_id)
            if report_id in registry:
                for r_dir in registry[report_id]:
                    if r_dir not in dirs_to_check:
                        dirs_to_check.insert(0, r_dir)
                        self._sidecar_store.add_search_dir(r_dir)
            for std_dir in ["/opt/coverage_tool", "/opt/coverage_reports/review", "/opt/coverage_reports/review_incremental", "/opt/coverage_reports"]:
                if os.path.isdir(std_dir) and std_dir not in dirs_to_check:
                    dirs_to_check.append(std_dir)
                    self._sidecar_store.add_search_dir(std_dir)

            # Try SidecarStore first (Chunked v2 + Legacy v1)
            ctx = self._sidecar_store.load_full_source_context(report_id, sidecar_file_hash)
            if ctx:
                if self.db_manager:
                    self._refresh_analysis_records(ctx, project_name, file_path, scope)
                self._context_cache[cache_key] = (now, ctx)
                self._prune_context_cache(now)
                return ctx

            for s_dir in dirs_to_check:
                sidecar_ctx = load_source_sidecar(s_dir, report_id, sidecar_file_hash)
                if sidecar_ctx:
                    if self.db_manager:
                        self._refresh_analysis_records(sidecar_ctx, project_name, file_path, scope)
                    self._context_cache[cache_key] = (now, sidecar_ctx)
                    self._prune_context_cache(now)
                    return sidecar_ctx

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
                self._prune_context_cache(now)
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
        """Update review state on existing lines using AnalysisOverlay."""
        db_file_hash = compute_db_file_path_hash(file_path)
        sidecar_file_hash = calc_sidecar_file_key(file_path)
        overlay = self.get_analysis_overlay(project_name, db_file_hash, file_path, review_scope)
        if not overlay:
            return

        pending_lines = []
        confirmed_count = 0

        for line in context.lines:
            rec = overlay.get_line_analysis(line.line_no)
            if rec:
                line.analysis_state = rec.get("status") or "未确认"
                line.reviewer = rec.get("reviewer") or line.suggested_reviewer
                line.is_draft = bool(rec.get("is_draft", False))
                line.coverage_method = rec.get("coverage_method") or ""
                line.uncovered_reason = rec.get("uncovered_reason") or ""
            else:
                line.analysis_state = "未确认"
                line.reviewer = line.suggested_reviewer
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

    def get_code_layout(
        self,
        project_name: str,
        file_path: str,
        report_id: str = "",
        review_scope: str = "full",
        content_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compute the CodeRegion layout for the file (O(1) fast metadata path when sidecar exists)."""
        t_start = time.perf_counter()
        db_file_hash = compute_db_file_path_hash(file_path)
        sidecar_file_hash = calc_sidecar_file_key(file_path)
        scope = (review_scope or "full").lower()

        # Fast Metadata Path (Items 1 & 13)
        if report_id and not content_override:
            registry = load_report_registry(report_id)
            if report_id in registry:
                for r_dir in registry[report_id]:
                    self._sidecar_store.add_search_dir(r_dir)

            meta = self._sidecar_store.load_metadata(report_id, sidecar_file_hash)
            if meta:
                overlay = self.get_analysis_overlay(project_name, db_file_hash, file_path, scope)

                raw_func_ranges = meta.get("function_ranges", [])
                func_ranges = [
                    FunctionRange(r[0], r[1], r[2]) if isinstance(r, (list, tuple)) else (
                        FunctionRange(r["start_line"], r["end_line"], r["name"]) if isinstance(r, dict) else r
                    )
                    for r in raw_func_ranges
                ]

                # Sidecar stores static facts only.  Recompute all dynamic
                # review state from the current overlay; never reuse an old
                # pending/confirmed snapshot.
                pending_lines = []
                confirmed_count = 0
                static_uncovered = set(meta.get("uncovered_lines", []))
                for line_no in static_uncovered:
                    rec = overlay.get_line_analysis(line_no) if overlay else None
                    status = rec.get("status") if rec else "未确认"
                    draft = bool(rec.get("is_draft")) if rec else False
                    if (not draft) and status in {"可覆盖", "无法覆盖", "冗余代码"}:
                        confirmed_count += 1
                    else:
                        pending_lines.append(line_no)

                total_lines = meta.get("total_lines", 0)
                regions = build_code_regions(
                    total_lines=total_lines,
                    pending_lines=pending_lines,
                    function_ranges=func_ranges,
                )
                elapsed_ms = (time.perf_counter() - t_start) * 1000.0
                expanded_count = sum(1 for r in regions if r.default_state == "expanded")
                expanded_lines = sum(r.line_count for r in regions if r.default_state == "expanded")
                collapsed_lines = sum(r.line_count for r in regions if r.default_state == "collapsed")

                return {
                    "project_name": project_name,
                    "file_path": file_path,
                    "report_id": report_id,
                    "total_lines": total_lines,
                    "total_uncovered_count": meta.get("static_total_uncovered_count", len(static_uncovered)),
                    "pending_line_count": len(pending_lines),
                    "confirmed_count": confirmed_count,
                    "regions": [r.to_dict() for r in regions],
                    "perf": {
                        "layout_build_ms": round(elapsed_ms, 2),
                        "pending_line_count": len(pending_lines),
                        "region_count": len(regions),
                        "expanded_region_count": expanded_count,
                        "expanded_line_count": expanded_lines,
                        "collapsed_line_count": collapsed_lines,
                    },
                }

        # Fallback to full context
        context = self.get_source_context(
            project_name=project_name,
            file_path=file_path,
            report_id=report_id,
            review_scope=scope,
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

        return {
            "project_name": project_name,
            "file_path": file_path,
            "report_id": context.report_id,
            "total_lines": context.total_lines,
            "total_uncovered_count": context.total_uncovered_count,
            "pending_line_count": len(context.pending_lines),
            "confirmed_count": context.confirmed_count,
            "regions": [r.to_dict() for r in regions],
            "perf": {
                "layout_build_ms": round(elapsed_ms, 2),
                "pending_line_count": len(context.pending_lines),
                "region_count": len(regions),
                "expanded_region_count": expanded_count,
                "expanded_line_count": expanded_lines,
                "collapsed_line_count": collapsed_lines,
            },
        }

    def get_code_lines_batch(
        self,
        project_name: str,
        file_path: str,
        ranges: Optional[List[Dict[str, int]]] = None,
        region_ids: Optional[List[str]] = None,
        load_default_expanded: bool = False,
        report_id: str = "",
        review_scope: str = "full",
        content_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Batch load line data for specified ranges or regions (Slice-based streaming)."""
        t_start = time.perf_counter()
        db_file_hash = compute_db_file_path_hash(file_path)
        sidecar_file_hash = calc_sidecar_file_key(file_path)
        scope = (review_scope or "full").lower()

        # 1. Determine verified ranges
        verified_ranges = []
        is_verified_default_batch = False

        if load_default_expanded or region_ids:
            layout = self.get_code_layout(
                project_name=project_name,
                file_path=file_path,
                report_id=report_id,
                review_scope=scope,
                content_override=content_override,
            )
            reg_map = {r["region_id"]: r for r in layout["regions"]}
            if load_default_expanded:
                for r in layout["regions"]:
                    if r["default_state"] == "expanded":
                        verified_ranges.append({"start_line": r["start_line"], "end_line": r["end_line"]})
                is_verified_default_batch = True
            elif region_ids:
                for rid in region_ids:
                    r = reg_map.get(str(rid))
                    if r and r["default_state"] == "expanded":
                        verified_ranges.append({"start_line": r["start_line"], "end_line": r["end_line"]})
                    else:
                        raise ValueError(f"Requested region '{rid}' is not a valid default expanded region")
                is_verified_default_batch = True
        elif ranges:
            if not isinstance(ranges, list):
                raise ValueError("ranges must be a list of ranges")
            total_req_lines = sum(int(r.get("end_line", 1)) - int(r.get("start_line", 1)) + 1 for r in ranges)
            if total_req_lines > 50000 or len(ranges) > 1000:
                raise ValueError("Requested lines exceeds maximum batch limit of 50000 lines")
            verified_ranges = ranges
        else:
            raise ValueError("ranges, region_ids, or load_default_expanded is required")

        overlay = self.get_analysis_overlay(project_name, db_file_hash, file_path, scope)

        # 2. Extract lines for each range
        batch_results = []
        total_returned_lines = 0

        for rng in verified_ranges:
            start_l = rng.get("start_line", 1)
            end_l = rng.get("end_line", 1)
            if start_l > end_l:
                continue

            lines_dicts = None
            if report_id and not content_override:
                lines_dicts = self._sidecar_store.load_lines_range(report_id, sidecar_file_hash, start_l, end_l)

            if lines_dicts is None:
                ctx = self.get_source_context(
                    project_name=project_name,
                    file_path=file_path,
                    report_id=report_id,
                    review_scope=scope,
                    content_override=content_override,
                )
                lines_slice = [l for l in ctx.lines if start_l <= l.line_no <= end_l]
                lines_dicts = [l.to_dict() for l in lines_slice]

            if overlay:
                for ld in lines_dicts:
                    l_no = ld.get("line_no", 0)
                    rec = overlay.get_line_analysis(l_no)
                    if rec:
                        ld["analysis_state"] = rec.get("status") or "未确认"
                        ld["reviewer"] = rec.get("reviewer") or ld.get("suggested_reviewer", "")
                        ld["is_draft"] = bool(rec.get("is_draft", False))
                        ld["coverage_method"] = rec.get("coverage_method") or ""
                        ld["uncovered_reason"] = rec.get("uncovered_reason") or ""
                    else:
                        ld["analysis_state"] = "未确认"
                        ld["reviewer"] = ld.get("suggested_reviewer", "")
                        ld["is_draft"] = False
                    is_pend = is_line_pending_analysis(
                        coverage_state=ld.get("coverage_state", "covered"),
                        analysis_state=ld.get("analysis_state", "未确认"),
                        is_draft=ld.get("is_draft", False),
                        fill_status="已填写" if rec and rec.get("status") else "未填写"
                    )
                    ld["is_pending_analysis"] = is_pend

            batch_results.append({
                "start_line": start_l,
                "end_line": end_l,
                "lines": lines_dicts,
            })
            total_returned_lines += len(lines_dicts)

        elapsed_ms = (time.perf_counter() - t_start) * 1000.0
        return {
            "project_name": project_name,
            "file_path": file_path,
            "report_id": report_id,
            "ranges": batch_results,
            "is_default_batch": is_verified_default_batch,
            "perf": {
                "batch_load_ms": round(elapsed_ms, 2),
                "total_lines_read": total_returned_lines,
                "total_lines_loaded": total_returned_lines,
                "verified_default_batch": is_verified_default_batch,
                "range_count": len(batch_results),
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
        """Fetch a single line range."""
        res = self.get_code_lines_batch(
            project_name=project_name,
            file_path=file_path,
            ranges=[{"start_line": start_line, "end_line": end_line}],
            report_id=report_id,
            review_scope=review_scope,
            content_override=content_override,
        )
        rngs = res.get("ranges", [])
        lines = rngs[0]["lines"] if rngs else []
        return {
            "project_name": project_name,
            "file_path": file_path,
            "report_id": report_id,
            "start_line": start_line,
            "end_line": end_line,
            "lines": lines,
            "perf": res.get("perf", {}),
        }
