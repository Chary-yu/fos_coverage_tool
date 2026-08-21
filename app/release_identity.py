"""
Release Identity Module (Item 18)
Provides unified versioning, commit SHA resolution, asset hashing, and release manifest generation.
"""

import os
import sys
import json
import hashlib
import subprocess
from typing import Dict, Any, Optional

from app.time_utils import utc_iso

DEFAULT_VERSION = "v11.7 2026-08-19"
DEFAULT_SCHEMA_VERSION = 2
RELEASE_MANIFEST_NAME = "release_manifest.json"

def _get_git_commit_sha(repo_root: str) -> str:
    """Safely get git commit SHA or return fallback."""
    try:
        res = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            stderr=subprocess.DEVNULL
        ).decode("utf-8").strip()
        if res:
            return res
    except Exception:
        pass
    return "0000000000000000000000000000000000000000"

def compute_asset_hash(file_paths: list) -> str:
    """Compute deterministic SHA256 hash over a list of static asset files."""
    hasher = hashlib.sha256()
    for fp in sorted(file_paths):
        if os.path.isfile(fp):
            with open(fp, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    hasher.update(chunk)
    return hasher.hexdigest()[:16]

def generate_release_identity(
    repo_root: Optional[str] = None,
    version: str = DEFAULT_VERSION,
    schema_version: int = DEFAULT_SCHEMA_VERSION,
    asset_files: Optional[list] = None
) -> Dict[str, Any]:
    """Generate a full release identity dictionary."""
    if repo_root is None:
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    
    commit_sha = _get_git_commit_sha(repo_root)
    
    if asset_files is None:
        asset_files = []
        candidates = [
            os.path.join(repo_root, "coverage_enhance.js"),
            os.path.join(repo_root, "coverage_enhance.css"),
            os.path.join(repo_root, "coverage_progress.js"),
            os.path.join(repo_root, "incremental_coverage.js"),
            os.path.join(repo_root, "incremental_developer_tasks.js"),
            os.path.join(repo_root, "web", "assets", "js", "coverage_enhance.js"),
            os.path.join(repo_root, "web", "assets", "css", "coverage_enhance.css"),
        ]
        for c in candidates:
            if os.path.isfile(c) and c not in asset_files:
                asset_files.append(c)
                
    asset_hash = compute_asset_hash(asset_files)
    clean_ver = version.split()[0]
    build_id = f"{clean_ver}-{commit_sha[:8]}-{asset_hash[:8]}"
    
    identity = {
        "version": version,
        "commit_sha": commit_sha,
        "build_id": build_id,
        "asset_hash": asset_hash,
        "schema_version": schema_version,
        "built_at": utc_iso()
    }
    return identity

def save_release_manifest(manifest_path: str, identity: Dict[str, Any]) -> None:
    """Write release identity to JSON file atomically."""
    temp_path = manifest_path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(identity, f, indent=2, ensure_ascii=False)
    os.replace(temp_path, manifest_path)

def load_release_manifest(manifest_path: str) -> Optional[Dict[str, Any]]:
    """Load release manifest if present."""
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None

def get_current_release_identity(repo_root: Optional[str] = None) -> Dict[str, Any]:
    """
    Get active release identity. Verifies that cached manifest matches current git HEAD commit SHA.
    Runtime verification is deliberately fail-closed. Build systems must create the
    manifest with ``save_release_manifest``; runtime never rewrites it to hide drift.
    """
    if repo_root is None:
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    manifest_path = os.path.join(repo_root, RELEASE_MANIFEST_NAME)
    manifest = load_release_manifest(manifest_path)
    
    current_head = _get_git_commit_sha(repo_root)
    if not manifest:
        raise RuntimeError("release_manifest.json is missing; generate it during build")
    expected = generate_release_identity(repo_root=repo_root, version=manifest.get("version", DEFAULT_VERSION))
    mismatches = [key for key in ("version", "commit_sha", "asset_hash", "schema_version")
                  if manifest.get(key) != expected.get(key)]
    if mismatches:
        raise RuntimeError("release identity drift: " + ", ".join(mismatches))
    return manifest

def verify_release_identity(repo_root: Optional[str] = None, target: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Verify runtime identity, optionally against an exact release target."""
    actual = get_current_release_identity(repo_root)
    if target:
        for key in ("version", "commit_sha", "build_id", "asset_hash", "schema_version"):
            if target.get(key) != actual.get(key):
                raise RuntimeError("target release mismatch: {}".format(key))
    return actual

if __name__ == "__main__":
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    ident = generate_release_identity(repo_root=root)
    mpath = os.path.join(root, RELEASE_MANIFEST_NAME)
    save_release_manifest(mpath, ident)
    print(f"[Release Identity] Generated and saved to {mpath}:")
    print(json.dumps(ident, indent=2))
