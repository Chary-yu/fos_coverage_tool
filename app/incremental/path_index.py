"""
LCOV Path Lookup Index Module (Item 12)
Provides fast O(1) multi-level path resolution with repository namespace isolation:
- Exact path matching
- Normalized path matching
- Unique safe suffix resolution
- Ambiguous suffix fail-closed protection
- Multi-repo namespace segregation
"""

from typing import Dict, Any, List, Tuple, Optional, Set

def normalize_lcov_path(path: str) -> str:
    """Normalize path separators and eliminate relative components."""
    clean = path.replace("\\", "/").strip().lstrip("/")
    parts = [p for p in clean.split("/") if p and p != "."]
    stack = []
    for p in parts:
        if p == "..":
            if stack:
                stack.pop()
        else:
            stack.append(p)
    return "/".join(stack)

class LCOVPathLookupIndex:
    def __init__(self, repo_target_paths: Dict[str, List[str]]):
        """
        repo_target_paths: Dict mapping repository/project name -> list of target source paths
        """
        self._repo_indexes: Dict[str, Dict[str, Any]] = {}
        for repo_name, paths in repo_target_paths.items():
            self._repo_indexes[repo_name] = self._build_single_repo_index(paths)

    def _build_single_repo_index(self, paths: List[str]) -> Dict[str, Any]:
        exact_map = {}
        normalized_map: Dict[str, Set[str]] = {}
        suffix_map: Dict[str, Set[str]] = {}
        basename_map: Dict[str, Set[str]] = {}
        
        for p in paths:
            norm = normalize_lcov_path(p)
            exact_map[p] = p
            normalized_map.setdefault(norm, set()).add(p)
            
            parts = norm.split("/")
            for i in range(len(parts) - 1):
                suf = "/".join(parts[i:])
                suffix_map.setdefault(suf, set()).add(p)
                
            bname = parts[-1] if parts else norm
            basename_map.setdefault(bname, set()).add(p)
            
        return {
            "exact": exact_map,
            "normalized": normalized_map,
            "suffix": suffix_map,
            "basename": basename_map
        }

    def resolve_path(self, repo_name: str, query_path: str) -> Tuple[Optional[str], str]:
        """
        Resolve path within the given repository namespace.
        Returns (resolved_path, match_type).
        """
        repo_idx = self._repo_indexes.get(repo_name)
        if not repo_idx:
            return None, "repo_not_found"
            
        # 1. Exact
        if query_path in repo_idx["exact"]:
            return repo_idx["exact"][query_path], "exact"
            
        # 2. Normalized
        norm = normalize_lcov_path(query_path)
        if norm in repo_idx["normalized"]:
            candidates = repo_idx["normalized"][norm]
            if len(candidates) == 1:
                return next(iter(candidates)), "normalized"
            return None, "ambiguous_normalized"
            
        # 3. Suffix
        parts = norm.split("/")
        for i in range(len(parts) - 1):
            suf = "/".join(parts[i:])
            if suf in repo_idx["suffix"]:
                candidates = repo_idx["suffix"][suf]
                if len(candidates) == 1:
                    return next(iter(candidates)), "unique_suffix"
                else:
                    return None, "ambiguous_suffix"
                    
        # 4. Basename only: Never treat as unique match
        bname = parts[-1] if parts else norm
        if bname in repo_idx["basename"]:
            return None, "basename_only_rejected"
            
        return None, "miss"
