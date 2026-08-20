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
import hashlib
import threading
from collections import OrderedDict
from typing import Dict, Any, List, Optional, Tuple

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from .source_reader import SourceContext, SourceLineDTO, calc_sidecar_file_key

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 2000
CHUNKED_SCHEMA_VERSION = 2

class SidecarStore:
    def __init__(
        self,
        search_dirs: Optional[List[str]] = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        asset_identity: str = "",
        max_cached_chunks: int = 128,
    ):
        self.search_dirs = [os.path.abspath(d) for d in (search_dirs or []) if os.path.isdir(d)]
        self.chunk_size = chunk_size
        self.asset_identity = str(asset_identity or "")
        self.max_cached_chunks = max(1, int(max_cached_chunks))
        self._cache_lock = threading.RLock()
        self._metadata_cache: Dict[Tuple[str, str, str], Tuple[Tuple[int, int], Dict[str, Any]]] = {}
        self._legacy_cache: Dict[Tuple[str, str, str], Tuple[Tuple[int, int], Dict[str, Any]]] = {}
        self._decoded_chunk_cache = OrderedDict()

    def add_search_dir(self, directory: str):
        if directory and os.path.isdir(directory):
            abs_d = os.path.abspath(directory)
            if abs_d not in self.search_dirs:
                self.search_dirs.append(abs_d)

    @staticmethod
    def _stat_signature(path: str) -> Optional[Tuple[int, int]]:
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return (
            int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1000000000))),
            int(stat.st_size),
        )

    def _asset_token(self, meta: Dict[str, Any], signature: Tuple[int, int]) -> str:
        """Return a cache token that changes when either report or sidecar changes."""
        content_hash = str(meta.get("content_hash") or "")
        return "{}|{}|{}".format(
            self.asset_identity,
            content_hash,
            "{}:{}".format(signature[0], signature[1]),
        )

    @staticmethod
    def _validate_chunk_inventory(chunk_dir: str, meta: Dict[str, Any]) -> None:
        declared_chunks = meta.get("chunks") or []
        for name in declared_chunks:
            safe_name = os.path.basename(str(name))
            chunk_path = os.path.join(chunk_dir, safe_name)
            if not os.path.isfile(chunk_path) or os.path.islink(chunk_path):
                raise FileNotFoundError("sidecar chunk is missing")

    def _load_chunked_metadata(
        self,
        report_id: str,
        file_path_hash: str,
        cache_dir: str,
        meta_path: str,
    ) -> Optional[Dict[str, Any]]:
        signature = self._stat_signature(meta_path)
        if signature is None or os.path.islink(meta_path):
            return None
        cache_key = (os.path.realpath(cache_dir), str(report_id), str(file_path_hash))
        with self._cache_lock:
            cached = self._metadata_cache.get(cache_key)
            if cached and cached[0] == signature:
                return cached[1]
            try:
                with open(meta_path, "r", encoding="utf-8") as stream:
                    meta = json.load(stream)
                if meta.get("report_id") and meta.get("report_id") != report_id:
                    raise ValueError("sidecar report identity mismatch")
                self._validate_chunk_inventory(cache_dir, meta)
                meta["_root"] = os.path.realpath(os.path.dirname(cache_dir))
                meta["_asset_identity"] = self._asset_token(meta, signature)
                self._metadata_cache[cache_key] = (signature, meta)
                return meta
            except Exception as exc:
                logger.warning("[SidecarStore] Error reading chunk meta %s: %s", meta_path, exc)
                self._metadata_cache.pop(cache_key, None)
                return None

    def _load_legacy_payload(
        self,
        report_id: str,
        file_path_hash: str,
        cache_dir: str,
        legacy_path: str,
    ) -> Optional[Dict[str, Any]]:
        signature = self._stat_signature(legacy_path)
        if signature is None or os.path.islink(legacy_path):
            return None
        cache_key = (os.path.realpath(cache_dir), str(report_id), str(file_path_hash))
        with self._cache_lock:
            cached = self._legacy_cache.get(cache_key)
            if cached and cached[0] == signature:
                return cached[1]
            try:
                with open(legacy_path, "r", encoding="utf-8") as stream:
                    data = json.load(stream)
                payload = {
                    "schema_version": 1,
                    "project_name": data.get("project_name", ""),
                    "file_path": data.get("file_path", ""),
                    "total_lines": data.get("total_lines", len(data.get("lines", []))),
                    "total_uncovered_count": data.get("total_uncovered_count", 0),
                    "uncovered_lines": data.get("uncovered_lines") or [
                        line.get("line_no") for line in data.get("lines", [])
                        if line.get("coverage_state") == "uncovered"
                    ],
                    "function_ranges": data.get("function_ranges", []),
                    "_lines": data.get("lines", []),
                    "_asset_identity": self._asset_token(data, signature),
                }
                self._legacy_cache[cache_key] = (signature, payload)
                return payload
            except Exception as exc:
                logger.warning("[SidecarStore] Error reading legacy sidecar %s: %s", legacy_path, exc)
                self._legacy_cache.pop(cache_key, None)
                return None

    def _load_decoded_chunk(
        self,
        report_id: str,
        file_path_hash: str,
        chunk_index: int,
        asset_identity: str,
        chunk_path: str,
    ) -> List[Dict[str, Any]]:
        chunk_signature = self._stat_signature(chunk_path)
        effective_identity = "{}|{}".format(asset_identity, chunk_signature)
        cache_key = (str(report_id), str(file_path_hash), int(chunk_index), effective_identity)
        with self._cache_lock:
            cached = self._decoded_chunk_cache.get(cache_key)
            if cached is not None:
                self._decoded_chunk_cache.move_to_end(cache_key)
                return cached
            if not os.path.isfile(chunk_path) or os.path.islink(chunk_path):
                raise FileNotFoundError("sidecar chunk is missing")
            with open(chunk_path, "r", encoding="utf-8") as stream:
                decoded = json.load(stream)
            if not isinstance(decoded, list):
                raise ValueError("sidecar chunk must contain a JSON list")
            self._decoded_chunk_cache[cache_key] = decoded
            self._decoded_chunk_cache.move_to_end(cache_key)
            while len(self._decoded_chunk_cache) > self.max_cached_chunks:
                self._decoded_chunk_cache.popitem(last=False)
            return decoded

    def _find_report_cache_dirs(self, report_id: str) -> List[str]:
        if (not report_id or "\\" in str(report_id)
                or any(part in ("", ".", "..") for part in str(report_id).split("/"))):
            return []
        cache_dirs = []
        for s_dir in self.search_dirs:
            p1 = os.path.join(s_dir, ".source_cache", report_id)
            p1_real = os.path.realpath(p1)
            cache_parent = os.path.realpath(os.path.join(s_dir, ".source_cache"))
            if (os.path.isdir(p1) and not os.path.islink(os.path.join(s_dir, ".source_cache"))
                    and not os.path.islink(p1)
                    and os.path.commonpath((cache_parent, p1_real)) == cache_parent
                    and p1 not in cache_dirs):
                cache_dirs.append(p1)
            p2 = os.path.join(s_dir, report_id, ".source_cache", report_id)
            p2_real = os.path.realpath(p2)
            p2_parent = os.path.realpath(os.path.join(s_dir, report_id, ".source_cache"))
            if (os.path.isdir(p2) and not os.path.islink(os.path.join(s_dir, report_id))
                    and not os.path.islink(os.path.join(s_dir, report_id, ".source_cache"))
                    and not os.path.islink(p2)
                    and os.path.commonpath((p2_parent, p2_real)) == p2_parent
                    and p2 not in cache_dirs):
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
            if os.path.isfile(meta_path) and not os.path.islink(chunk_dir) and not os.path.islink(meta_path):
                meta = self._load_chunked_metadata(
                    report_id, file_path_hash, chunk_dir, meta_path
                )
                if meta:
                    return meta

            # 2. Try Legacy v1 .source.json
            legacy_path = os.path.join(c_dir, f"{file_path_hash}.source.json")
            if os.path.isfile(legacy_path) and not os.path.islink(legacy_path):
                payload = self._load_legacy_payload(
                    report_id, file_path_hash, c_dir, legacy_path
                )
                if payload:
                    return {key: value for key, value in payload.items() if key != "_lines"}
        return None

    def load_lines_ranges(
        self,
        report_id: str,
        file_path_hash: str,
        ranges: List[Tuple[int, int]],
    ) -> Optional[List[List[Dict[str, Any]]]]:
        """Load multiple logical ranges while decoding each physical chunk once."""
        normalized = []
        for item in ranges or []:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise ValueError("each line range must contain start_line and end_line")
            start_line, end_line = int(item[0]), int(item[1])
            if start_line < 1 or end_line < start_line:
                raise ValueError("invalid line range")
            normalized.append((start_line, end_line))
        if not normalized:
            return []

        cache_dirs = self._find_report_cache_dirs(report_id)
        for c_dir in cache_dirs:
            chunk_dir = os.path.join(c_dir, file_path_hash)
            meta_path = os.path.join(chunk_dir, "meta.json")
            if (os.path.isdir(chunk_dir) and not os.path.islink(chunk_dir)
                    and os.path.isfile(meta_path) and not os.path.islink(meta_path)):
                meta = self._load_chunked_metadata(
                    report_id, file_path_hash, chunk_dir, meta_path
                )
                if not meta:
                    continue
                try:
                    chunk_size = int(meta.get("chunk_size") or self.chunk_size)
                    needed = set()
                    for start_line, end_line in normalized:
                        needed.update(range(
                            (start_line - 1) // chunk_size,
                            (end_line - 1) // chunk_size + 1,
                        ))
                    decoded = {}
                    declared_chunks = {
                        os.path.basename(str(name)) for name in (meta.get("chunks") or [])
                    }
                    total_chunks = int(meta.get("total_chunks") or 0)
                    for chunk_index in sorted(needed):
                        chunk_start = chunk_index * chunk_size
                        chunk_end = chunk_start + chunk_size - 1
                        chunk_path = os.path.join(
                            chunk_dir, "lines-{:06d}-{:06d}.json".format(
                                chunk_start, chunk_end
                            )
                        )
                        if total_chunks and chunk_index >= total_chunks:
                            decoded[chunk_index] = []
                            continue
                        chunk_name = "lines-{:06d}-{:06d}.json".format(
                            chunk_start, chunk_end
                        )
                        if declared_chunks and chunk_name not in declared_chunks:
                            decoded[chunk_index] = []
                            continue
                        decoded[chunk_index] = self._load_decoded_chunk(
                            report_id, file_path_hash, chunk_index,
                            str(meta.get("_asset_identity") or ""), chunk_path,
                        )
                    result = []
                    for start_line, end_line in normalized:
                        rows = []
                        first = (start_line - 1) // chunk_size
                        last = (end_line - 1) // chunk_size
                        for chunk_index in range(first, last + 1):
                            rows.extend(
                                line for line in decoded[chunk_index]
                                if start_line <= int(line.get("line_no") or 0) <= end_line
                            )
                        rows.sort(key=lambda line: int(line.get("line_no") or 0))
                        result.append(rows)
                    return result
                except Exception as exc:
                    logger.warning("[SidecarStore] Error reading chunked lines %s: %s", chunk_dir, exc)

            legacy_path = os.path.join(c_dir, f"{file_path_hash}.source.json")
            if os.path.isfile(legacy_path) and not os.path.islink(legacy_path):
                payload = self._load_legacy_payload(
                    report_id, file_path_hash, c_dir, legacy_path
                )
                if payload:
                    all_lines = payload.get("_lines") or []
                    return [[
                        line for line in all_lines
                        if start_line <= int(line.get("line_no") or 0) <= end_line
                    ] for start_line, end_line in normalized]
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
        batches = self.load_lines_ranges(
            report_id, file_path_hash, [(start_line, end_line)]
        )
        return batches[0] if batches is not None else None

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
        declared_hash = meta.get("content_hash")
        if declared_hash:
            actual_hash = hashlib.sha256(
                json.dumps(lines_dicts, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            if actual_hash != declared_hash:
                logger.warning("[SidecarStore] Content hash mismatch for %s/%s", report_id, file_path_hash)
                return None
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
        chunk_names = []
        
        # 1. Write chunk files
        for c_idx in range(total_chunks):
            start_idx = c_idx * c_size
            end_idx = min(start_idx + c_size, total_lines)
            chunk_slice = all_lines_dict[start_idx:end_idx]
            
            c_start_line = start_idx
            c_end_line = start_idx + c_size - 1
            chunk_filename = f"lines-{c_start_line:06d}-{c_end_line:06d}.json"
            chunk_names.append(chunk_filename)
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
            "report_id": report_id,
            "project_name": context.project_name,
            "file_path": context.file_path,
            "file_path_hash": file_path_hash,
            "total_lines": total_lines,
            "uncovered_lines": [l.line_no for l in context.lines if l.coverage_state == "uncovered"],
            "static_total_uncovered_count": sum(1 for l in context.lines if l.coverage_state == "uncovered"),
            "function_ranges": func_ranges_json,
            "chunk_size": c_size,
            "total_chunks": total_chunks,
            "chunks": chunk_names,
            "content_hash": hashlib.sha256(
                json.dumps(all_lines_dict, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
        }
        meta_filepath = os.path.join(cache_dir, "meta.json")
        tmp_meta = meta_filepath + ".tmp"
        with open(tmp_meta, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        os.replace(tmp_meta, meta_filepath)
        
        return cache_dir
