"""Canonical incremental path service facade.

All metadata emitted by an incremental report (review lines, suggested
reviewers, and LCOV function ranges) must use the same path-resolution rules.
Keeping the mapping lookup here prevents individual callers from quietly
reintroducing unsafe ``endswith`` matching.
"""

from typing import Any, Dict, Optional, Tuple

from app.incremental.path_index import LCOVPathLookupIndex, normalize_lcov_path


class IncrementalService:
    def __init__(self, repo_target_paths):
        self.path_index = LCOVPathLookupIndex(repo_target_paths)

    @staticmethod
    def _mapping_value(mapping: Dict[str, Any], resolved_path: str) -> Any:
        if resolved_path in mapping:
            return mapping[resolved_path]

        # The index preserves the original key while its normalized lookup
        # removes leading/relative path noise.  Resolve that small final
        # representation mismatch without weakening the ambiguity decision.
        normalized = normalize_lcov_path(resolved_path)
        for key, value in mapping.items():
            if normalize_lcov_path(str(key)) == normalized:
                return value
        return None

    def resolve_mapping_value(
        self,
        query_path: str,
        mapping: Optional[Dict[str, Any]],
        repo_name: str = "default",
    ) -> Tuple[Any, str]:
        """Resolve one value using exact/normalized/unique-suffix semantics.

        ``None`` is returned for misses and ambiguous/basename-only matches.
        The accompanying match type is useful for diagnostics and tests.
        """
        if not mapping:
            return None, "empty"

        index = LCOVPathLookupIndex({repo_name: list(mapping.keys())})
        resolved, match_type = index.resolve_path(repo_name, str(query_path or ""))
        if resolved is None:
            return None, match_type
        return self._mapping_value(mapping, resolved), match_type
