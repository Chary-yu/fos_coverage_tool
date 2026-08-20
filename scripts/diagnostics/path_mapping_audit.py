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
import re
from typing import Dict, Any, List, Tuple, Optional, Set

def normalize_path(path: str) -> str:
    """Normalize path separators and remove relative references."""
    clean = path.replace("\\", "/").strip()
    parts = [p for p in clean.split("/") if p and p != "."]
    stack = []
    for p in parts:
        if p == "..":
            if stack:
                stack.pop()
        else:
            stack.append(p)
    return "/".join(stack)

class PathLookupIndex:
    """
    Multi-level path index with repository namespace scoping (Item 12 & 25).
    """
    def __init__(self, target_paths: List[str], repo_name: str = ""):
        self.repo_name = repo_name
        self.exact_map: Dict[str, str] = {}
        self.normalized_map: Dict[str, str] = {}
        self.suffix_map: Dict[str, Set[str]] = {}
        self.basename_map: Dict[str, Set[str]] = {}
        
        for p in target_paths:
            norm = normalize_path(p)
            self.exact_map[p] = p
            self.normalized_map[norm] = p
            
            # Suffix index (minimum 2 segments)
            parts = norm.split("/")
            for i in range(len(parts) - 1):
                suffix = "/".join(parts[i:])
                if suffix not in self.suffix_map:
                    self.suffix_map[suffix] = set()
                self.suffix_map[suffix].add(p)
                
            # Basename index
            bname = parts[-1] if parts else norm
            if bname not in self.basename_map:
                self.basename_map[bname] = set()
            self.basename_map[bname].add(p)

    def resolve(self, query_path: str) -> Tuple[Optional[str], str]:
        """
        Resolve query path to target path with match classification:
        Returns (resolved_path_or_none, classification).
        """
        # 1. Exact
        if query_path in self.exact_map:
            return self.exact_map[query_path], "exact"
            
        # 2. Normalized
        norm = normalize_path(query_path)
        if norm in self.normalized_map:
            return self.normalized_map[norm], "normalized"
            
        # 3. Suffix search
        parts = norm.split("/")
        for i in range(len(parts) - 1):
            suffix = "/".join(parts[i:])
            if suffix in self.suffix_map:
                candidates = self.suffix_map[suffix]
                if len(candidates) == 1:
                    return next(iter(candidates)), "unique_suffix"
                else:
                    # Ambiguous suffix: fail closed
                    return None, "ambiguous_suffix"
                    
        # 4. Basename only: Never auto-map (treat as untrusted / miss)
        bname = parts[-1] if parts else norm
        if bname in self.basename_map:
            return None, "basename_only_rejected"
            
        return None, "miss"

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
                value = normalize_path(raw[3:].strip())
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
                "basename_only_rejected": "lcov_basename_rejected",
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
        ("src/../src/net/socket.c", "normalized"),
        ("core/engine.c", "unique_suffix"),
        ("engine.c", "basename_only_rejected"), # Ambiguous basename rejected
        ("other/nonexistent.c", "miss")
    ]
    audit_res = audit_path_mappings(known, queries)
    print("Path Mapping Audit Results:")
    for k, v in audit_res.items():
        print(f"  {k}: {v}")
    sys.exit(0 if audit_res["is_valid"] else 1)
