"""
Path Mapping Audit Module (Item 25)
Audits source and report path matching against data quality rules:
- Classifies mappings: exact, normalized, unique_suffix, ambiguous_suffix, basename_only, miss
- Enforces strict safety rules:
  * basename_only is NEVER treated as trusted unique mapping
  * ambiguous_suffix must fail-closed (no false match)
  * multi-repo namespaces must not collide or cross-match
"""

import os
import sys
from typing import Dict, Any, List, Tuple, Optional, Set

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.incremental.path_index import (  # noqa: E402
    LCOVPathLookupIndex, normalize_lcov_path,
)


def normalize_path(path: str) -> str:
    """Use the production LCOV normalizer and reject parent traversal."""
    return normalize_lcov_path(path)

class PathLookupIndex:
    """
    Multi-level path index with repository namespace scoping (Item 12 & 25).
    """
    def __init__(self, target_paths: List[str], repo_name: str = ""):
        self.repo_name = repo_name or "__audit__"
        self._index = LCOVPathLookupIndex({self.repo_name: list(target_paths)})

    def resolve(self, query_path: str) -> Tuple[Optional[str], str]:
        """
        Resolve query path to target path with match classification:
        Returns (resolved_path_or_none, classification).
        """
        return self._index.resolve_path(self.repo_name, query_path)

def audit_path_mappings(known_paths: List[str], test_queries: List[Tuple[str, str]]) -> Dict[str, Any]:
    """
    Run path mapping audit.
    test_queries: List of (query_path, expected_classification)
    """
    idx = PathLookupIndex(known_paths)
    stats = {
        "exact": 0,
        "normalized": 0,
        "unique_suffix": 0,
        "ambiguous_suffix": 0,
        "ambiguous_normalized": 0,
        "basename_only_rejected": 0,
        "miss": 0,
        "violations": []
    }
    
    for q, expected in test_queries:
        res, cls = idx.resolve(q)
        stats[cls] = stats.get(cls, 0) + 1
        if expected and cls != expected:
            stats["violations"].append(f"Query '{q}' expected '{expected}' but got '{cls}'")
            
    stats["is_valid"] = (len(stats["violations"]) == 0)
    return stats


def parse_lcov_source_paths(info_path: str) -> List[str]:
    """Read real ``SF:`` entries from an LCOV file without invoking lcov."""
    paths = []
    with open(info_path, "r", encoding="utf-8", errors="replace") as stream:
        for raw in stream:
            if raw.startswith("SF:"):
                # Preserve the raw LCOV identity.  In particular, do not
                # discard ``..`` here; the shared resolver must classify it
                # as invalid so the audit can fail closed.
                value = raw[3:].strip()
                if value:
                    paths.append(value)
    return sorted(set(paths))


def audit_lcov_paths(known_paths: List[str], info_files: List[str]) -> Dict[str, Any]:
    """Resolve every source path in real LCOV inputs and fail closed on misses."""
    index = PathLookupIndex(known_paths)
    stats = {
        "lcov_files": [os.path.abspath(path) for path in info_files],
        "lcov_source_count": 0,
        "lcov_exact": 0,
        "lcov_normalized": 0,
        "lcov_unique_suffix": 0,
        "lcov_ambiguous": 0,
        "lcov_miss": 0,
        "lcov_basename_rejected": 0,
        "lcov_invalid_path": 0,
        "violations": [],
    }
    for info_file in info_files:
        for query in parse_lcov_source_paths(info_file):
            stats["lcov_source_count"] += 1
            resolved, classification = index.resolve(query)
            key = {
                "exact": "lcov_exact",
                "normalized": "lcov_normalized",
                "unique_suffix": "lcov_unique_suffix",
                "ambiguous_suffix": "lcov_ambiguous",
                "ambiguous_normalized": "lcov_ambiguous",
                "basename_only_rejected": "lcov_basename_rejected",
                "invalid_path": "lcov_invalid_path",
                "miss": "lcov_miss",
            }.get(classification, "lcov_miss")
            stats[key] += 1
            if resolved is None:
                stats["violations"].append(
                    "LCOV source '{}' resolved as {}".format(query, classification)
                )
    stats["is_valid"] = bool(stats["lcov_source_count"]) and not stats["violations"]
    return stats

if __name__ == "__main__":
    known = [
        "src/core/engine.c",
        "src/utils/engine.c",
        "src/net/socket.c",
        "include/common/config.h"
    ]
    queries = [
        ("src/core/engine.c", "exact"),
        ("src/../src/net/socket.c", "invalid_path"),
        ("core/engine.c", "unique_suffix"),
        ("engine.c", "basename_only_rejected"), # Ambiguous basename rejected
        ("other/nonexistent.c", "miss")
    ]
    audit_res = audit_path_mappings(known, queries)
    print("Path Mapping Audit Results:")
    for k, v in audit_res.items():
        print(f"  {k}: {v}")
    sys.exit(0 if audit_res["is_valid"] else 1)
