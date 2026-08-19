"""
Unified Sidecar Store Module (Items 13 & 14)
Supports dual-format sidecar storage:
1. Chunked Sidecar (v2 format):
   .source_cache/<report_id>/<file_key>/
     ├── meta.json
     ├── lines-000000-001999.json
     └── ...
2. Legacy Sidecar (v1 format):
   .source_cache/<report_id>/<file_key>.source.json

Enforces read-order rule: Chunked v2 -> Legacy v1 -> Fail closed.
"""

import os
import sys
import json
import logging
from typing import Dict, Any, List, Optional, Tuple

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from source_reader import SourceContext, SourceLineDTO, calc_sidecar_file_key

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 2000
CHUNKED_SCHEMA_VERSION = 2

class SidecarStore:
    def __init__(self, search_dirs: Optional[List[str]] = None, chunk_size: int = DEFAULT_CHUNK_SIZE):
        self.search_dirs = [os.path.abspath(d) for d in (search_dirs or []) if os.path.isdir(d)]
        self.chunk_size = chunk_size

    def add_search_dir(self, directory: str):
        if directory and os.path.isdir(directory):
            abs_d = os.path.abspath(directory)
            if abs_d not in self.search_dirs:
                self.search_dirs.append(abs_d)

    def _find_report_cache_dirs(self, report_id: str) -> List[str]:
        cache_dirs = []
        for s_dir in self.search_dirs:
            p1 = os.path.join(s_dir, ".source_cache", report_id)
            if os.path.isdir(p1) and p1 not in cache_dirs:
                cache_dirs.append(p1)
            p2 = os.path.join(s_dir, report_id, ".source_cache", report_id)
            if os.path.isdir(p2) and p2 not in cache_dirs:
                cache_dirs.append(p2)
        return cache_dirs

    def load_metadata(self, report_id: str, file_path_hash: str) -> Optional[Dict[str, Any]]:
        """
        Load metadata only for fast layout calculation.
        Reads meta.json (Chunked v2) or extracts metadata from legacy .source.json (v1).
        """
        cache_dirs = self._find_report_cache_dirs(report_id)
        for c_dir in cache_dirs:
            # 1. Try Chunked v2 meta.json
            chunk_dir = os.path.join(c_dir, file_path_hash)
            meta_path = os.path.join(chunk_dir, "meta.json")
            if os.path.isfile(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception as e:
                    logger.warning(f"[SidecarStore] Error reading chunk meta {meta_path}: {e}")

            # 2. Try Legacy v1 .source.json
            legacy_path = os.path.join(c_dir, f"{file_path_hash}.source.json")
            if os.path.isfile(legacy_path):
                try:
                    with open(legacy_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    return {
                        "schema_version": 1,
                        "project_name": data.get("project_name", ""),
                        "file_path": data.get("file_path", ""),
                        "total_lines": data.get("total_lines", len(data.get("lines", []))),
                        "total_uncovered_count": data.get("total_uncovered_count", 0),
                        "pending_lines": data.get("pending_lines", []),
                        "confirmed_count": data.get("confirmed_count", 0),
                        "function_ranges": data.get("function_ranges", [])
                    }
                except Exception as e:
                    logger.warning(f"[SidecarStore] Error reading legacy sidecar {legacy_path}: {e}")
        return None

    def load_lines_range(
        self,
        report_id: str,
        file_path_hash: str,
        start_line: int,
        end_line: int
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Load only the lines covering [start_line, end_line] without reading full file if chunked.
        """
        cache_dirs = self._find_report_cache_dirs(report_id)
        for c_dir in cache_dirs:
            # 1. Try Chunked v2
            chunk_dir = os.path.join(c_dir, file_path_hash)
            meta_path = os.path.join(chunk_dir, "meta.json")
            if os.path.isdir(chunk_dir) and os.path.isfile(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    c_size = meta.get("chunk_size", self.chunk_size)
                    start_chunk_idx = (start_line - 1) // c_size
                    end_chunk_idx = (end_line - 1) // c_size
                    
                    matched_lines = []
                    for c_idx in range(start_chunk_idx, end_chunk_idx + 1):
                        c_start = c_idx * c_size
                        c_end = c_start + c_size - 1
                        chunk_file = os.path.join(chunk_dir, f"lines-{c_start:06d}-{c_end:06d}.json")
                        if os.path.isfile(chunk_file):
                            with open(chunk_file, "r", encoding="utf-8") as cf:
                                c_lines = json.load(cf)
                            for l_dict in c_lines:
                                l_no = l_dict.get("line_no", 0)
                                if start_line <= l_no <= end_line:
                                    matched_lines.append(l_dict)
                    return matched_lines
                except Exception as e:
                    logger.warning(f"[SidecarStore] Error reading chunked lines {chunk_dir}: {e}")

            # 2. Try Legacy v1
            legacy_path = os.path.join(c_dir, f"{file_path_hash}.source.json")
            if os.path.isfile(legacy_path):
                try:
                    with open(legacy_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    all_lines = data.get("lines", [])
                    matched_lines = [
                        l for l in all_lines 
                        if start_line <= l.get("line_no", 0) <= end_line
                    ]
                    return matched_lines
                except Exception as e:
                    logger.warning(f"[SidecarStore] Error reading legacy lines {legacy_path}: {e}")
        return None

    def load_full_source_context(self, report_id: str, file_path_hash: str) -> Optional[SourceContext]:
        """
        Load complete SourceContext from Chunked v2 or Legacy v1.
        """
        meta = self.load_metadata(report_id, file_path_hash)
        if not meta:
            return None
        total_l = meta.get("total_lines", 0)
        lines_dicts = self.load_lines_range(report_id, file_path_hash, 1, total_l)
        if lines_dicts is None:
            return None
        
        source_lines = [SourceLineDTO.from_dict(d) for d in lines_dicts]
        ctx = SourceContext(
            project_name=meta.get("project_name", ""),
            file_path=meta.get("file_path", ""),
            lines=source_lines,
            function_ranges=meta.get("function_ranges", []),
            report_id=report_id,
            pending_lines=meta.get("pending_lines"),
            confirmed_count=meta.get("confirmed_count", 0)
        )
        return ctx

    def save_chunked_sidecar(
        self,
        output_dir: str,
        report_id: str,
        file_path_hash: str,
        context: SourceContext
    ) -> str:
        """
        Write new Chunked v2 format sidecar to output_dir/.source_cache/<report_id>/<file_key>/
        """
        cache_dir = os.path.join(output_dir, ".source_cache", report_id, file_path_hash)
        os.makedirs(cache_dir, exist_ok=True)
        
        all_lines_dict = [line.to_dict() for line in context.lines]
        total_lines = len(all_lines_dict)
        c_size = self.chunk_size
        total_chunks = (total_lines + c_size - 1) // c_size if total_lines > 0 else 0
        
        # 1. Write chunk files
        for c_idx in range(total_chunks):
            start_idx = c_idx * c_size
            end_idx = min(start_idx + c_size, total_lines)
            chunk_slice = all_lines_dict[start_idx:end_idx]
            
            c_start_line = start_idx
            c_end_line = start_idx + c_size - 1
            chunk_filename = f"lines-{c_start_line:06d}-{c_end_line:06d}.json"
            chunk_filepath = os.path.join(cache_dir, chunk_filename)
            
            tmp_chunk = chunk_filepath + ".tmp"
            with open(tmp_chunk, "w", encoding="utf-8") as f:
                json.dump(chunk_slice, f, ensure_ascii=False)
            os.replace(tmp_chunk, chunk_filepath)
            
        # 2. Write meta.json
        func_ranges_json = [
            r.to_dict() if hasattr(r, "to_dict") else (
                {"start_line": r.start_line, "end_line": r.end_line, "name": r.name} if hasattr(r, "start_line") else (
                    {"start_line": r[0], "end_line": r[1], "name": r[2]} if isinstance(r, (list, tuple)) and len(r) >= 3 else r
                )
            )
            for r in (context.function_ranges or [])
        ]
        meta = {
            "schema_version": CHUNKED_SCHEMA_VERSION,
            "project_name": context.project_name,
            "file_path": context.file_path,
            "file_path_hash": file_path_hash,
            "total_lines": total_lines,
            "total_uncovered_count": context.total_uncovered_count,
            "pending_lines": context.pending_lines,
            "confirmed_count": context.confirmed_count,
            "function_ranges": func_ranges_json,
            "chunk_size": c_size,
            "total_chunks": total_chunks
        }
        meta_filepath = os.path.join(cache_dir, "meta.json")
        tmp_meta = meta_filepath + ".tmp"
        with open(tmp_meta, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        os.replace(tmp_meta, meta_filepath)
        
        return cache_dir
