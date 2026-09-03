"""
Release Identity Module (Item 18)
Provides unified versioning, commit SHA resolution, asset hashing, and release manifest generation.
"""

import os
import sys
import json
import hashlib
import re
import subprocess
from typing import Dict, Any, Optional

from app.time_utils import utc_iso

DEFAULT_VERSION = "v11.7 2026-08-19"
DEFAULT_SCHEMA_VERSION = 2
ASSET_MANIFEST_VERSION = 1
RELEASE_MANIFEST_NAME = "release_manifest.json"
_FULL_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{40}$")

# Keep this list in one place for both build-time generation and any release
# governance checker.  The root-level files are retained compatibility assets;
# the web/assets files are the canonical browser sources.
DEFAULT_RELEASE_ASSET_RELATIVE_PATHS = (
    "coverage_enhance.js",
    "coverage_enhance.css",
    "coverage_progress.js",
    "incremental_coverage.js",
    "incremental_developer_tasks.js",
    "pending_snapshot.js",
    "web/assets/js/coverage_enhance.js",
    "web/assets/js/coverage_progress.js",
    "web/assets/js/incremental_coverage.js",
    "web/assets/js/incremental_developer_tasks.js",
    "web/assets/js/pending_snapshot.js",
    "web/assets/css/coverage_enhance.css",
    "coverage_progress.html",
    "web/templates/coverage_progress.html",
)

def _get_git_commit_sha(repo_root: str) -> str:
    """Safely get the exact checkout SHA, or an empty value on failure."""
    try:
        res = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            stderr=subprocess.DEVNULL
        ).decode("utf-8").strip()
        if is_valid_commit_sha(res):
            return res
    except Exception:
        pass
    return ""


def is_valid_commit_sha(value: str) -> bool:
    """Return true only for a concrete, non-placeholder full Git SHA."""
    value = str(value or "").strip()
    return bool(_FULL_COMMIT_SHA.fullmatch(value)) and set(value.lower()) != {"0"}


def _has_git_metadata(repo_root: str) -> bool:
    """Whether runtime can legitimately use checkout metadata as evidence."""
    return os.path.exists(os.path.join(os.path.abspath(repo_root), ".git"))


def assert_clean_git_checkout(repo_root: str) -> None:
    """Require a clean Git checkout before creating a release manifest."""
    repo_root = os.path.abspath(repo_root)
    if not _has_git_metadata(repo_root):
        raise RuntimeError(
            "release build requires .git metadata for a clean exact checkout"
        )
    try:
        output = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repo_root,
            stderr=subprocess.STDOUT,
        ).decode("utf-8", "replace")
    except Exception as exc:
        raise RuntimeError(
            "release build cannot inspect Git worktree: {}: {}".format(
                type(exc).__name__, exc
            )
        )
    if output.strip():
        raise RuntimeError("release build requires a clean exact checkout")


def _default_asset_files(repo_root: str) -> list:
    files = []
    missing = []
    for relative in DEFAULT_RELEASE_ASSET_RELATIVE_PATHS:
        candidate = os.path.join(repo_root, *relative.split("/"))
        if os.path.isfile(candidate) and candidate not in files:
            files.append(candidate)
        elif not os.path.isfile(candidate):
            missing.append(relative)
    if missing:
        raise RuntimeError(
            "required release asset(s) missing: " + ", ".join(missing)
        )
    return files


def _manifest_build_id(manifest: Dict[str, Any]) -> str:
    version = str(manifest.get("version") or "")
    commit_sha = str(manifest.get("commit_sha") or "")
    asset_hash = str(manifest.get("asset_hash") or "")
    return "{}-{}-{}".format(
        version.split()[0], commit_sha[:8], asset_hash[:8]
    )

def _asset_relative_path(file_path, repo_root=None):
    file_path = os.path.abspath(str(file_path))
    if repo_root is None:
        return file_path.replace(os.sep, "/")
    relative = os.path.relpath(file_path, os.path.abspath(repo_root))
    return relative.replace(os.sep, "/")


def build_asset_manifest(file_paths: list, repo_root: Optional[str] = None) -> list:
    """Return a fail-closed, path/size/content manifest for release assets."""
    entries = []
    seen = set()
    root = os.path.realpath(os.path.abspath(repo_root)) if repo_root else None
    for raw_path in file_paths or ():
        path = str(raw_path)
        if root and not os.path.isabs(path):
            path = os.path.join(root, path)
        path = os.path.realpath(os.path.abspath(path))
        if root:
            try:
                contained = os.path.commonpath((root, path)) == root
            except ValueError:
                contained = False
            if not contained:
                raise RuntimeError(
                    "release asset outside repository root: {}".format(path)
                )
        relative = _asset_relative_path(path, root)
        if relative in seen:
            raise RuntimeError("duplicate release asset path: " + relative)
        seen.add(relative)
        if not os.path.isfile(path):
            raise RuntimeError("required release asset missing: " + relative)
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        entries.append({
            "path": relative,
            "size": int(os.path.getsize(path)),
            "sha256": digest.hexdigest(),
        })
    return sorted(entries, key=lambda item: item["path"])


def _hash_asset_manifest(asset_manifest):
    canonical = json.dumps(
        asset_manifest or [], sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def compute_asset_hash(file_paths: list, repo_root: Optional[str] = None) -> str:
    """Hash canonical path, size and content records for all assets."""
    return _hash_asset_manifest(build_asset_manifest(file_paths, repo_root))

def generate_release_identity(
    repo_root: Optional[str] = None,
    version: str = DEFAULT_VERSION,
    schema_version: int = DEFAULT_SCHEMA_VERSION,
    asset_files: Optional[list] = None,
    commit_sha: Optional[str] = None,
    build_provenance: str = "git-checkout",
) -> Dict[str, Any]:
    """Generate a full release identity dictionary."""
    if repo_root is None:
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    
    requested_commit_sha = (
        None if commit_sha is None else str(commit_sha).strip()
    )
    if _has_git_metadata(repo_root):
        checkout_commit_sha = _get_git_commit_sha(repo_root)
        if not is_valid_commit_sha(checkout_commit_sha):
            raise RuntimeError(
                "release build cannot resolve the checked-out commit SHA"
            )
        if (requested_commit_sha and
                requested_commit_sha.lower() != checkout_commit_sha.lower()):
            raise RuntimeError(
                "explicit commit SHA does not match checked-out HEAD"
            )
        commit_sha = checkout_commit_sha
    else:
        commit_sha = str(
            _get_git_commit_sha(repo_root)
            if requested_commit_sha is None else requested_commit_sha
        ).strip()
    if not is_valid_commit_sha(commit_sha):
        raise RuntimeError("release build requires a concrete commit SHA")

    provenance = str(build_provenance or "git-checkout")
    if provenance == "release-build" and _has_git_metadata(repo_root):
        assert_clean_git_checkout(repo_root)
    
    if asset_files is None:
        asset_files = _default_asset_files(repo_root)

    asset_manifest = build_asset_manifest(asset_files, repo_root)
    asset_hash = _hash_asset_manifest(asset_manifest)
    clean_ver = version.split()[0]
    build_id = f"{clean_ver}-{commit_sha[:8]}-{asset_hash[:8]}"
    
    identity = {
        "version": version,
        "commit_sha": commit_sha,
        "build_id": build_id,
        "asset_hash": asset_hash,
        "asset_manifest_version": ASSET_MANIFEST_VERSION,
        "asset_count": len(asset_manifest),
        "asset_manifest_hash": asset_hash,
        "asset_manifest": asset_manifest,
        "schema_version": schema_version,
        "build_provenance": provenance,
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
    if not manifest:
        raise RuntimeError("release_manifest.json is missing; generate it during build")
    required = (
        "version", "commit_sha", "build_id", "asset_hash", "schema_version",
        "asset_manifest_version", "asset_count", "asset_manifest_hash",
        "asset_manifest",
    )
    missing = [key for key in required if manifest.get(key) in (None, "")]
    if missing:
        raise RuntimeError("release identity manifest is incomplete: " + ", ".join(missing))
    if not is_valid_commit_sha(manifest.get("commit_sha")):
        raise RuntimeError(
            "release identity manifest has no concrete commit SHA; exact commit SHA required"
        )

    declared_assets = manifest.get("asset_manifest")
    if not isinstance(declared_assets, list):
        raise RuntimeError("release identity asset_manifest is invalid")
    if _has_git_metadata(repo_root):
        asset_files = _default_asset_files(repo_root)
    else:
        asset_files = [
            os.path.join(repo_root, *str(item.get("path") or "").split("/"))
            for item in declared_assets
            if isinstance(item, dict) and item.get("path")
        ]
    if (str(manifest.get("build_provenance") or "") == "release-build" and
            not asset_files):
        raise RuntimeError("release build has no declared assets")
    actual_manifest = build_asset_manifest(asset_files, repo_root)
    actual_asset_hash = _hash_asset_manifest(actual_manifest)
    mismatches = []
    if declared_assets != actual_manifest:
        mismatches.append("asset_manifest")
    if str(manifest.get("asset_hash")) != actual_asset_hash:
        mismatches.append("asset_hash")
    if str(manifest.get("asset_manifest_hash")) != actual_asset_hash:
        mismatches.append("asset_manifest_hash")
    if int(manifest.get("asset_count") or 0) != len(actual_manifest):
        mismatches.append("asset_count")
    if int(manifest.get("asset_manifest_version") or 0) != ASSET_MANIFEST_VERSION:
        mismatches.append("asset_manifest_version")
    if str(manifest.get("build_id")) != _manifest_build_id(manifest):
        mismatches.append("build_id")
    if _has_git_metadata(repo_root):
        current_head = _get_git_commit_sha(repo_root)
        if str(manifest.get("commit_sha")) != current_head:
            mismatches.append("commit_sha")
    else:
        # Artifact mode intentionally trusts only the immutable build manifest
        # for source identity.  It must still contain a concrete SHA-shaped
        # value; a build system must never turn a missing Git checkout into a
        # silently accepted all-zero release.
        commit = str(manifest.get("commit_sha") or "")
        if not is_valid_commit_sha(commit):
            raise RuntimeError("release artifact manifest has no exact commit SHA")
    if manifest.get("version") in (None, ""):
        mismatches.append("version")
    if manifest.get("schema_version") is None:
        mismatches.append("schema_version")
    if mismatches:
        raise RuntimeError("release identity drift: " + ", ".join(mismatches))
    return manifest


def resolve_runtime_release_root(repo_root: Optional[str] = None) -> str:
    """Resolve the immutable release root from an application bundle path.

    vfoswind starts ``CURRENT/app/enhance_coverage.py`` with
    ``WorkingDirectory=CURRENT/app``.  Release identity and publication
    manifests live one directory above that bundle, so runtime code must not
    mistake the application subdirectory for the release root.
    """
    requested = os.path.realpath(os.path.abspath(
        repo_root or os.getcwd()
    ))
    if os.path.isfile(os.path.join(requested, RELEASE_MANIFEST_NAME)):
        return requested
    if os.path.basename(requested) == "app":
        parent = os.path.dirname(requested)
        if os.path.isfile(os.path.join(parent, RELEASE_MANIFEST_NAME)):
            return parent
    return requested

def verify_release_identity(repo_root: Optional[str] = None, target: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Verify runtime identity, optionally against an exact release target."""
    actual = get_current_release_identity(repo_root)
    if target:
        for key in (
                "version", "commit_sha", "build_id", "asset_hash", "schema_version",
                "asset_manifest_version", "asset_count", "asset_manifest_hash",
                "asset_manifest"):
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
