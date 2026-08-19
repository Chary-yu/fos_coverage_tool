"""
Directory Signature Incremental Hash Module (Item 11)
Calculates deterministic directory signatures using incremental per-file manifests:
- Reuses cached file SHA256 if (path, size, mtime_ns) match
- Re-hashes only modified or new files
- Graceful fallback on manifest corruption
"""

import os
import sys
import json
import hashlib
from typing import Dict, Any, List, Tuple, Optional

MANIFEST_NAME = ".dir_manifest.json"

def compute_file_sha256(filepath: str) -> str:
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()

def calculate_directory_signature_incremental(
    dir_path: str,
    file_extensions: Tuple[str, ...] = (".gcov.html",)
) -> Tuple[str, Dict[str, Any]]:
    """
    Calculate directory signature using incremental manifest optimization.
    Returns (directory_sha256, manifest_data).
    """
    manifest_path = os.path.join(dir_path, MANIFEST_NAME)
    cached_manifest = {}
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                cached_manifest = json.load(f).get("files", {})
        except Exception:
            cached_manifest = {}

    current_files = {}
    overall_hasher = hashlib.sha256()
    
    # Collect all matching files
    file_list = []
    for root, _, files in os.walk(dir_path):
        for fname in files:
            if fname.endswith(file_extensions) and fname != MANIFEST_NAME:
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, dir_path).replace("\\", "/")
                file_list.append((rel_path, full_path))
                
    # Sort deterministically
    file_list.sort(key=lambda x: x[0])
    
    for rel_path, full_path in file_list:
        try:
            st = os.stat(full_path)
            size = st.st_size
            mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
            
            cached_entry = cached_manifest.get(rel_path)
            if (cached_entry and 
                cached_entry.get("size") == size and 
                cached_entry.get("mtime_ns") == mtime_ns and 
                cached_entry.get("sha256")):
                f_sha = cached_entry["sha256"]
            else:
                f_sha = compute_file_sha256(full_path)
                
            current_files[rel_path] = {
                "size": size,
                "mtime_ns": mtime_ns,
                "sha256": f_sha
            }
            overall_hasher.update(f"{rel_path}|{size}|{f_sha}\n".encode("utf-8"))
        except Exception:
            pass
            
    dir_signature = overall_hasher.hexdigest()
    
    # Save updated manifest
    manifest_data = {
        "version": 1,
        "directory_signature": dir_signature,
        "files": current_files
    }
    
    try:
        tmp_path = manifest_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f, indent=2)
        os.replace(tmp_path, manifest_path)
    except Exception:
        pass
        
    return dir_signature, manifest_data
