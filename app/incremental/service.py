"""Canonical incremental path service facade.

All metadata emitted by an incremental report (review lines, suggested
reviewers, and LCOV function ranges) must use the same path-resolution rules.
Keeping the mapping lookup here prevents individual callers from quietly
reintroducing unsafe ``endswith`` matching.
"""

import threading
from typing import Any, Dict, Optional, Tuple

from app.incremental.path_index import LCOVPathLookupIndex


class IncrementalService:
    def __init__(self, repo_target_paths=None):
        self.path_index = LCOVPathLookupIndex(repo_target_paths or {})
        # A report reuses the same path-keyed metadata map for every source
        # file.  Keep one immutable lookup index per map instead of rebuilding
        # the full suffix tables for every resolve() call.
        self._mapping_indexes = {}
        self._mapping_index_lock = threading.RLock()

    @staticmethod
    def _mapping_value(mapping: Dict[str, Any], resolved_path: str) -> Any:
        # LCOVPathLookupIndex always returns the original mapping key, so this
        # final lookup is O(1).  Do not scan every key to normalize it again.
        return mapping.get(resolved_path)

    def prepare_mapping(self, mapping: Optional[Dict[str, Any]], repo_name: str = "default"):
        """Build and retain one read-only index for a metadata mapping.

        The mapping is expected to remain unchanged for the lifetime of the
        report.  Keeping the mapping object in the cache also prevents Python
        object-id reuse from returning an index for a different mapping.
        """
        if not mapping:
            return None
        cache_key = (id(mapping), repo_name)
        with self._mapping_index_lock:
            cached = self._mapping_indexes.get(cache_key)
            if cached is not None and cached[0] is mapping:
                return cached[1]
            index = LCOVPathLookupIndex({repo_name: list(mapping.keys())})
            self._mapping_indexes[cache_key] = (mapping, index)
            return index

    def resolve_mapping_value(
        self,
        query_path: str,
        mapping: Optional[Dict[str, Any]],
        repo_name: str = "default",
        mapping_index=None,
    ) -> Tuple[Any, str]:
        """Resolve one value using exact/normalized/unique-suffix semantics.

        ``None`` is returned for misses and ambiguous/basename-only matches.
        The accompanying match type is useful for diagnostics and tests.
        """
        if not mapping:
            return None, "empty"

        index = mapping_index or self.prepare_mapping(mapping, repo_name)
        resolved, match_type = index.resolve_path(repo_name, str(query_path or ""))
        if resolved is None:
            return None, match_type
        return self._mapping_value(mapping, resolved), match_type
