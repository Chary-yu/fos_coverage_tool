"""
Analysis Overlay & data_version Cache Module (Item 1)
Decouples static source file structure (StaticSourceContext) from mutable user review state (AnalysisOverlay).
Cache keys: (project_name, file_path_hash, review_scope, data_version)
"""

import time
import threading
from typing import Dict, Any, List, Optional, Tuple

class AnalysisOverlay:
    def __init__(
        self,
        project_name: str,
        file_path_hash: str,
        review_scope: str,
        data_version: int,
        records: List[Dict[str, Any]],
        incremental_lines: Optional[set] = None
    ):
        self.project_name = project_name
        self.file_path_hash = file_path_hash
        self.review_scope = review_scope
        self.data_version = data_version
        self.created_at = time.time()
        self.incremental_lines = set(incremental_lines or [])
        
        # Build line -> record mapping
        self.analysis_by_line: Dict[int, Dict[str, Any]] = {}
        for r in records:
            line_num = int(r.get("line_number", 0))
            if line_num > 0:
                self.analysis_by_line[line_num] = r

    def get_line_analysis(self, line_number: int) -> Optional[Dict[str, Any]]:
        return self.analysis_by_line.get(line_number)

class AnalysisOverlayCache:
    """Thread-safe LRU cache for AnalysisOverlay instances keyed by data_version."""
    def __init__(self, max_entries: int = 128, ttl_seconds: float = 300.0):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._cache: Dict[Tuple[str, str, str, int], AnalysisOverlay] = {}
        self._lock = threading.Lock()

    def get(self, project_name: str, file_path_hash: str, review_scope: str, data_version: int) -> Optional[AnalysisOverlay]:
        key = (project_name, file_path_hash, review_scope, int(data_version))
        with self._lock:
            overlay = self._cache.get(key)
            if overlay is None:
                return None
            if (time.time() - overlay.created_at) > self.ttl_seconds:
                del self._cache[key]
                return None
            return overlay

    def put(self, overlay: AnalysisOverlay) -> None:
        key = (overlay.project_name, overlay.file_path_hash, overlay.review_scope, int(overlay.data_version))
        with self._lock:
            if len(self._cache) >= self.max_entries:
                # Evict oldest
                oldest_k = min(self._cache.keys(), key=lambda k: self._cache[k].created_at)
                del self._cache[oldest_k]
            self._cache[key] = overlay

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
