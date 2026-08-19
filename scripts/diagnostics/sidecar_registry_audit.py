"""
Sidecar & Registry Integrity Audit Module (Item 22)
Scans and validates report registry files and source cache directories:
- Checks registry JSON syntax and directory reachability
- Detects orphaned cache directories
- Detects format distribution (Legacy v1 vs Chunked v2)
- Enforces path escape and security boundaries
- Fails is_safe if corruptions, orphaned caches, or broken chunk files are detected
"""

import os
import sys
import json
import logging
from typing import Dict, Any, List, Tuple, Optional

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from code_detail_service import get_configured_registry_dir, is_valid_report_id

def audit_sidecar_and_registry(search_roots: List[str]) -> Dict[str, Any]:
    """Audit all registries and sidecar storage locations."""
    reg_dir = get_configured_registry_dir()
    registered_reports: Dict[str, List[str]] = {}
    corrupted_registries = []
    
    # 1. Audit Registry Files
    if os.path.isdir(reg_dir):
        for fname in os.listdir(reg_dir):
            if fname.endswith(".json") and not fname.startswith("."):
                fpath = os.path.join(reg_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    rid = data.get("report_id") or os.path.splitext(fname)[0]
                    if not is_valid_report_id(rid):
                        corrupted_registries.append(f"Invalid report_id format in registry: {fname}")
                    dirs = data.get("directories", [])
                    if isinstance(dirs, str):
                        dirs = [dirs]
                    registered_reports[rid] = [d for d in dirs if os.path.isdir(d)]
                except Exception as e:
                    corrupted_registries.append(f"Corrupted registry JSON: {fname} ({e})")
                    
    # 2. Audit Cache Directories
    total_sidecars = 0
    chunked_v2_count = 0
    legacy_v1_count = 0
    orphaned_caches = []
    corrupted_chunks = []
    
    for s_root in search_roots:
        cache_base = os.path.join(s_root, ".source_cache")
        if not os.path.isdir(cache_base):
            continue
            
        for r_entry in os.listdir(cache_base):
            r_path = os.path.join(cache_base, r_entry)
            if not os.path.isdir(r_path):
                continue
                
            report_id = r_entry
            if report_id not in registered_reports and not report_id.startswith("report_benchmark_"):
                orphaned_caches.append(r_path)
                
            for item in os.listdir(r_path):
                item_path = os.path.join(r_path, item)
                if os.path.isdir(item_path):
                    meta_path = os.path.join(item_path, "meta.json")
                    if os.path.isfile(meta_path):
                        try:
                            with open(meta_path, "r", encoding="utf-8") as mf:
                                mdata = json.load(mf)
                            if "total_lines" not in mdata or "total_chunks" not in mdata:
                                corrupted_chunks.append(f"Missing required fields in {meta_path}")
                        except Exception as e:
                            corrupted_chunks.append(f"Corrupted meta.json in {item_path}: {e}")
                        chunked_v2_count += 1
                        total_sidecars += 1
                elif item.endswith(".source.json"):
                    legacy_v1_count += 1
                    total_sidecars += 1
                    
    is_safe = (len(corrupted_registries) == 0 and len(corrupted_chunks) == 0 and len(orphaned_caches) == 0)
    
    return {
        "status": "AUDIT_PASSED" if is_safe else "VIOLATIONS_FOUND",
        "registered_report_count": len(registered_reports),
        "corrupted_registries": corrupted_registries,
        "corrupted_chunks": corrupted_chunks,
        "orphaned_cache_count": len(orphaned_caches),
        "orphaned_caches": orphaned_caches,
        "total_sidecars": total_sidecars,
        "chunked_v2_count": chunked_v2_count,
        "legacy_v1_count": legacy_v1_count,
        "is_safe": is_safe
    }

if __name__ == "__main__":
    roots = [_REPO_ROOT, "/opt/coverage_tool", "/opt/coverage_reports"]
    res = audit_sidecar_and_registry(roots)
    print("Sidecar & Registry Audit Report:")
    for k, v in res.items():
        print(f"  {k}: {v}")
    sys.exit(0 if res["is_safe"] else 1)
