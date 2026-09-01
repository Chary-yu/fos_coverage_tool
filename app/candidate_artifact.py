"""Candidate artifact provenance and content manifest.

``release_manifest.json`` describes an already published immutable release.
This module describes the input artifact *before* publication and binds its
contents to the exact release identity supplied by the build pipeline.

The implementation intentionally uses only Python 3.6-compatible standard
library APIs because the manifest is consumed by the Python 3.6 release lane.
"""

from __future__ import print_function

import hashlib
import json
import os
import subprocess

from app.release_identity import is_valid_commit_sha
from app.time_utils import utc_iso


CANDIDATE_ARTIFACT_MANIFEST_VERSION = 1
CANDIDATE_ARTIFACT_MANIFEST_NAME = "candidate_artifact_manifest.json"
ARTIFACT_DIRECTORIES = ("reports", "assets", "registry")


def _real(path):
    return os.path.realpath(os.path.abspath(str(path)))


def _inside(root, path):
    try:
        return os.path.commonpath((_real(root), _real(path))) == _real(root)
    except ValueError:
        return False


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _assert_no_symlinks(root):
    root = _real(root)
    if not os.path.isdir(root):
        raise ValueError("candidate artifact root is not a directory: {}".format(root))
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        for name in sorted(dirnames + filenames):
            path = os.path.join(directory, name)
            if os.path.islink(path):
                raise ValueError("candidate artifact may not contain symlinks: {}".format(path))


def _relative(root, path):
    return os.path.relpath(_real(path), _real(root)).replace(os.sep, "/")


def _inventory(root, manifest_path):
    root = _real(root)
    manifest_path = _real(manifest_path)
    if not _inside(root, manifest_path):
        raise ValueError("candidate artifact manifest must be inside candidate_root")
    _assert_no_symlinks(root)
    entries = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(dirnames)
        filenames = sorted(filenames)
        for name in filenames:
            path = os.path.join(directory, name)
            if _real(path) == manifest_path:
                continue
            if not os.path.isfile(path):
                raise ValueError("candidate artifact entry is not a regular file: {}".format(path))
            entries.append({
                "path": _relative(root, path),
                "size": int(os.path.getsize(path)),
                "sha256": _sha256(path),
            })
    return sorted(entries, key=lambda item: item["path"])


def _directory_hash(entries, directory):
    prefix = directory.rstrip("/") + "/"
    return _canonical_hash([
        item for item in entries if item.get("path", "").startswith(prefix)
    ])


def _identity_snapshot(identity):
    identity = dict(identity or {})
    commit_sha = str(identity.get("commit_sha") or "").strip()
    build_id = str(identity.get("build_id") or "").strip()
    if not is_valid_commit_sha(commit_sha):
        raise ValueError("candidate artifact manifest requires an exact commit_sha")
    if not build_id:
        raise ValueError("candidate artifact manifest requires a build_id")
    snapshot = {}
    for key in (
            "version", "commit_sha", "build_id", "asset_hash",
            "schema_version", "asset_manifest_version", "asset_count",
            "asset_manifest_hash"):
        if identity.get(key) not in (None, ""):
            snapshot[key] = identity.get(key)
    snapshot["commit_sha"] = commit_sha
    snapshot["build_id"] = build_id
    return snapshot


def _verify_checkout_head(candidate_root, commit_sha):
    """Use checkout metadata when an artifact is built from a Git tree."""
    if not os.path.exists(os.path.join(_real(candidate_root), ".git")):
        return
    try:
        observed = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=_real(candidate_root), stderr=subprocess.STDOUT,
        ).decode("ascii", "replace").strip()
    except (OSError, subprocess.CalledProcessError, UnicodeError) as exc:
        raise ValueError("candidate artifact Git identity is unavailable: {}".format(exc))
    if observed.lower() != str(commit_sha).lower():
        raise ValueError(
            "candidate artifact Git HEAD does not match release identity commit_sha"
        )


def _build_payload(candidate_root, identity, manifest_path):
    candidate_root = _real(candidate_root)
    identity_snapshot = _identity_snapshot(identity)
    _verify_checkout_head(candidate_root, identity_snapshot["commit_sha"])
    manifest_path = _real(manifest_path)
    for directory in ARTIFACT_DIRECTORIES:
        if not os.path.isdir(os.path.join(candidate_root, directory)):
            raise ValueError("candidate artifact is missing {}/".format(directory))
    entries = _inventory(candidate_root, manifest_path)
    directory_hashes = {
        directory: _directory_hash(entries, directory)
        for directory in ARTIFACT_DIRECTORIES
    }
    return {
        "artifact_manifest_version": CANDIDATE_ARTIFACT_MANIFEST_VERSION,
        "commit_sha": identity_snapshot["commit_sha"],
        "build_id": identity_snapshot["build_id"],
        "files": entries,
        "directory_sha256": directory_hashes,
        "reports_sha256": directory_hashes["reports"],
        "assets_sha256": directory_hashes["assets"],
        "registry_sha256": directory_hashes["registry"],
        "artifact_sha256": _canonical_hash(entries),
    }


def _write_json(path, payload):
    path = os.path.abspath(path)
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    temporary = "{}.tmp-{}".format(path, os.getpid())
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        try:
            os.fsync(stream.fileno())
        except OSError:
            pass
    os.replace(temporary, path)


class CandidateArtifactManifest(object):
    """Build and verify the provenance manifest for a candidate artifact."""

    @classmethod
    def build(cls, candidate_root, release_identity, output_path=None):
        candidate_root = _real(candidate_root)
        manifest_path = _real(output_path or os.path.join(
            candidate_root, CANDIDATE_ARTIFACT_MANIFEST_NAME
        ))
        identity = _identity_snapshot(release_identity)
        payload = _build_payload(candidate_root, identity, manifest_path)
        result = dict(payload)
        result.update(identity)
        result["release_identity"] = identity
        result["generated_at"] = utc_iso()
        result["manifest_path"] = _relative(candidate_root, manifest_path)
        _write_json(manifest_path, result)
        return result

    @classmethod
    def load(cls, candidate_root, manifest_path=None):
        candidate_root = _real(candidate_root)
        path = _real(manifest_path or os.path.join(
            candidate_root, CANDIDATE_ARTIFACT_MANIFEST_NAME
        ))
        if not _inside(candidate_root, path) or not os.path.isfile(path):
            raise ValueError("candidate artifact manifest is missing: {}".format(path))
        if os.path.islink(path):
            raise ValueError("candidate artifact manifest may not be a symlink")
        try:
            with open(path, "r", encoding="utf-8") as stream:
                value = json.load(stream)
        except (OSError, ValueError) as exc:
            raise ValueError("candidate artifact manifest is unreadable: {}".format(exc))
        if not isinstance(value, dict):
            raise ValueError("candidate artifact manifest must be a JSON object")
        return value, path

    @classmethod
    def verify(cls, candidate_root, release_identity, candidate_sha="",
               manifest_path=None):
        candidate_root = _real(candidate_root)
        manifest, path = cls.load(candidate_root, manifest_path)
        if int(manifest.get("artifact_manifest_version") or 0) != \
                CANDIDATE_ARTIFACT_MANIFEST_VERSION:
            raise ValueError("unsupported candidate artifact manifest version")
        expected_identity = dict(release_identity or {})
        if candidate_sha:
            if expected_identity.get("commit_sha") and \
                    str(expected_identity.get("commit_sha")).lower() != str(candidate_sha).lower():
                raise ValueError("candidate_sha does not match release identity commit_sha")
            expected_identity["commit_sha"] = candidate_sha
        expected_snapshot = _identity_snapshot(expected_identity)
        declared_identity = manifest.get("release_identity") or {}
        if not isinstance(declared_identity, dict):
            raise ValueError("candidate artifact release_identity is invalid")
        for key, expected in expected_snapshot.items():
            if manifest.get(key) != expected:
                raise ValueError(
                    "candidate artifact manifest {} does not match release identity".format(key)
                )
            if declared_identity.get(key) != expected:
                raise ValueError(
                    "candidate artifact release_identity {} does not match".format(key)
                )
        observed = _build_payload(candidate_root, expected_snapshot, path)
        for key in (
                "commit_sha", "build_id", "files", "directory_sha256",
                "reports_sha256", "assets_sha256", "registry_sha256",
                "artifact_sha256"):
            if manifest.get(key) != observed.get(key):
                raise ValueError(
                    "candidate artifact manifest {} does not match candidate_root".format(key)
                )
        return manifest
