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
import re
import subprocess

from app.release_identity import is_valid_commit_sha
from app.time_utils import utc_iso


CANDIDATE_ARTIFACT_MANIFEST_VERSION = 2
CANDIDATE_ARTIFACT_MANIFEST_NAME = "candidate_artifact_manifest.json"
CANDIDATE_BUILD_ATTESTATION_VERSION = 1
CANDIDATE_BUILD_ATTESTATION_NAME = "candidate_build_attestation.json"
CANDIDATE_BUILD_RECEIPT_VERSION = 1
CANDIDATE_BUILD_RECEIPT_NAME = "candidate_build_receipt.json"
PROVENANCE_SCHEMA_VERSION = 1
TRUSTED_CI_PROVENANCE_CLASS = "trusted-ci-build"
SERVED_ROOT_BOOTSTRAP_PROVENANCE_CLASS = "served-root-bootstrap"
TRUSTED_PROVENANCE_CLASSES = (
    TRUSTED_CI_PROVENANCE_CLASS, SERVED_ROOT_BOOTSTRAP_PROVENANCE_CLASS,
)
ARTIFACT_DIRECTORIES = ("reports", "assets", "registry")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_GIT_TREE_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_WORKFLOW_RUN_ID = re.compile(r"^[1-9][0-9]*$")


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


def _inventory(root, manifest_path=None, excluded_paths=None):
    root = _real(root)
    excluded = set()
    if manifest_path:
        manifest_path = _real(manifest_path)
        if not _inside(root, manifest_path):
            raise ValueError("candidate artifact manifest must be inside candidate_root")
        excluded.add(manifest_path)
    for excluded_path in excluded_paths or ():
        excluded_path = _real(excluded_path)
        if not _inside(root, excluded_path):
            raise ValueError("candidate artifact metadata must be inside candidate_root")
        excluded.add(excluded_path)
    _assert_no_symlinks(root)
    entries = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(dirnames)
        filenames = sorted(filenames)
        for name in filenames:
            path = os.path.join(directory, name)
            if _real(path) in excluded:
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


def identity_manifest_sha256(identity):
    """Digest the exact identity fields that a Candidate is allowed to claim."""
    return _canonical_hash(_identity_snapshot(identity))


def build_directory_input_manifest_sha256(root):
    """Hash the complete input file inventory for a non-Git source tree."""
    entries = _inventory(root)
    return _canonical_hash({
        "manifest_version": 1,
        "files": entries,
    })


def _git_source_manifest(source_repo_root, source_commit_sha, source_tree_sha):
    try:
        raw = subprocess.check_output(
            ["git", "ls-tree", "-r", "--full-tree", "-z", "HEAD"],
            cwd=source_repo_root, stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("source Git file manifest is unavailable: {}".format(exc))
    entries = []
    for record in raw.decode("utf-8", "replace").split("\0"):
        if not record:
            continue
        try:
            metadata, relative_path = record.split("\t", 1)
            mode, object_type, object_sha = metadata.split(" ", 2)
        except ValueError:
            raise ValueError("source Git file manifest is malformed")
        entries.append({
            "mode": mode,
            "type": object_type,
            "object_sha": object_sha,
            "path": relative_path,
        })
    return {
        "manifest_version": 1,
        "source_commit_sha": source_commit_sha,
        "source_tree_sha": source_tree_sha,
        "files": sorted(entries, key=lambda item: item["path"]),
    }


def _source_provenance(value, require_artifact_sha=False,
                       require_trusted=False):
    if not isinstance(value, dict):
        raise ValueError("candidate artifact source provenance is required")
    provenance = dict(value)
    provenance.setdefault("provenance_schema_version", PROVENANCE_SCHEMA_VERSION)
    provenance.setdefault(
        "build_workflow_run_id", provenance.get("build_workflow_identity")
    )
    provenance.setdefault("build_workflow_run_attempt", "1")
    provenance.setdefault("build_workflow_sha", "")
    provenance.setdefault(
        "build_input_manifest_sha256", provenance.get("source_manifest_sha256")
    )
    required = (
        "provenance_class", "source_commit_sha", "source_tree_sha",
        "build_workflow_identity", "source_manifest_sha256",
    )
    missing = [key for key in required if provenance.get(key) in (None, "")]
    if missing:
        raise ValueError(
            "candidate artifact source provenance is missing: " + ", ".join(missing)
        )
    if not is_valid_commit_sha(provenance.get("source_commit_sha")):
        raise ValueError("candidate artifact source_commit_sha must be an exact commit SHA")
    if not _GIT_TREE_SHA.fullmatch(str(provenance.get("source_tree_sha"))):
        raise ValueError("candidate artifact source_tree_sha must be an exact Git tree SHA")
    if not _SHA256.fullmatch(str(provenance.get("source_manifest_sha256"))):
        raise ValueError("candidate artifact source_manifest_sha256 is invalid")
    if int(provenance.get("provenance_schema_version") or 0) != \
            PROVENANCE_SCHEMA_VERSION:
        raise ValueError("unsupported candidate artifact provenance schema")
    workflow_sha = str(provenance.get("build_workflow_sha") or "")
    if workflow_sha and not is_valid_commit_sha(workflow_sha):
        raise ValueError("candidate artifact build_workflow_sha is invalid")
    input_manifest_sha = str(provenance.get("build_input_manifest_sha256") or "")
    if input_manifest_sha and not _SHA256.fullmatch(input_manifest_sha):
        raise ValueError("candidate artifact build_input_manifest_sha256 is invalid")
    if provenance.get("worktree_clean") is not True:
        raise ValueError("candidate artifact source worktree must be clean")
    if require_artifact_sha and not _SHA256.fullmatch(
            str(provenance.get("candidate_artifact_sha256") or "")):
        raise ValueError("candidate artifact candidate_artifact_sha256 is invalid")
    if require_trusted:
        if provenance.get("provenance_class") not in TRUSTED_PROVENANCE_CLASSES:
            raise ValueError(
                "candidate artifact requires trusted provenance class; got {}".format(
                    provenance.get("provenance_class")
                )
            )
        required_trusted = (
            "build_workflow_run_id", "build_workflow_sha",
            "build_workflow_run_attempt", "build_input_manifest_sha256",
        )
        missing = [key for key in required_trusted
                   if provenance.get(key) in (None, "")]
        if missing:
            raise ValueError(
                "trusted candidate artifact provenance is missing: " +
                ", ".join(missing)
            )
        if not is_valid_commit_sha(provenance.get("build_workflow_sha")):
            raise ValueError("trusted candidate artifact build_workflow_sha is invalid")
        if not _SHA256.fullmatch(str(provenance.get("build_input_manifest_sha256"))):
            raise ValueError(
                "trusted candidate artifact build_input_manifest_sha256 is invalid"
            )
        if provenance.get("provenance_class") == TRUSTED_CI_PROVENANCE_CLASS:
            if not _WORKFLOW_RUN_ID.fullmatch(
                    str(provenance.get("build_workflow_run_id"))):
                raise ValueError(
                    "trusted candidate artifact build_workflow_run_id must be a positive numeric ID"
                )
            if not _WORKFLOW_RUN_ID.fullmatch(
                    str(provenance.get("build_workflow_run_attempt"))):
                raise ValueError(
                    "trusted candidate artifact build_workflow_run_attempt must be a positive numeric ID"
                )
    return provenance


def verify_trusted_build_policy(provenance, workflow_identity, workflow_sha):
    """Verify a Candidate against an independently supplied CI trust policy."""
    normalized = _source_provenance(provenance, require_trusted=True)
    if normalized.get("provenance_class") != TRUSTED_CI_PROVENANCE_CLASS:
        raise ValueError(
            "trusted build policy accepts trusted-ci-build provenance only"
        )
    expected_identity = str(workflow_identity or "").strip()
    expected_sha = str(workflow_sha or "").strip()
    if not expected_identity or not expected_sha:
        raise ValueError(
            "trusted build workflow identity and SHA are required"
        )
    if not is_valid_commit_sha(expected_sha):
        raise ValueError("trusted build workflow SHA is not an exact commit SHA")
    if str(normalized.get("build_workflow_identity") or "").strip() != \
            expected_identity:
        raise ValueError(
            "Candidate build workflow identity does not match trusted build identity"
        )
    if str(normalized.get("build_workflow_sha") or "").lower() != \
            expected_sha.lower():
        raise ValueError(
            "Candidate build workflow SHA does not match trusted build identity"
        )
    return normalized


def build_git_source_provenance(source_repo_root, release_identity,
                                build_workflow_identity, build_workflow_run_id="",
                                build_workflow_sha="", build_workflow_run_attempt=""):
    """Capture immutable source checkout provenance for a packaged artifact."""
    source_repo_root = _real(source_repo_root)
    if not os.path.exists(os.path.join(source_repo_root, ".git")):
        raise ValueError("source-repo-root must contain .git metadata")
    workflow = str(build_workflow_identity or "").strip()
    if not workflow:
        raise ValueError("build_workflow_identity is required")
    workflow_run_id = str(build_workflow_run_id or workflow).strip()
    workflow_run_attempt = str(build_workflow_run_attempt or "1").strip()
    workflow_sha = str(build_workflow_sha or "").strip()
    if workflow_sha and not is_valid_commit_sha(workflow_sha):
        raise ValueError("build_workflow_sha must be an exact commit SHA")
    if workflow_sha and not _WORKFLOW_RUN_ID.fullmatch(workflow_run_id):
        raise ValueError("build_workflow_run_id must be a positive numeric ID")
    if workflow_sha and not _WORKFLOW_RUN_ID.fullmatch(workflow_run_attempt):
        raise ValueError(
            "build_workflow_run_attempt must be a positive numeric ID"
        )
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source_repo_root,
            stderr=subprocess.STDOUT,
        ).decode("ascii", "replace").strip()
        tree = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=source_repo_root,
            stderr=subprocess.STDOUT,
        ).decode("ascii", "replace").strip()
        status = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=source_repo_root, stderr=subprocess.STDOUT,
        ).decode("utf-8", "replace")
    except (OSError, subprocess.CalledProcessError, UnicodeError) as exc:
        raise ValueError("source Git provenance is unavailable: {}".format(exc))
    expected_commit = str((release_identity or {}).get("commit_sha") or "").strip()
    if not is_valid_commit_sha(head) or head.lower() != expected_commit.lower():
        raise ValueError("source Git HEAD does not match release identity commit_sha")
    if not _GIT_TREE_SHA.fullmatch(tree):
        raise ValueError("source Git tree SHA is invalid")
    if status.strip():
        raise ValueError("source Git worktree must be clean")
    source_manifest = _git_source_manifest(source_repo_root, head, tree)
    source_manifest_sha = _canonical_hash(source_manifest)
    build_input_manifest_sha = _canonical_hash({
        "manifest_version": 1,
        "source_manifest_sha256": source_manifest_sha,
        "source_commit_sha": head,
        "source_tree_sha": tree,
        "build_workflow_run_id": workflow_run_id,
        "build_workflow_run_attempt": workflow_run_attempt,
        "build_workflow_sha": workflow_sha,
    })
    return {
        "provenance_class": (
            TRUSTED_CI_PROVENANCE_CLASS if workflow_sha else "git-checkout"
        ),
        "provenance_schema_version": PROVENANCE_SCHEMA_VERSION,
        "source_commit_sha": head,
        "source_tree_sha": tree,
        "worktree_clean": True,
        "build_workflow_identity": workflow,
        "build_workflow_run_id": workflow_run_id,
        "build_workflow_run_attempt": workflow_run_attempt,
        "build_workflow_sha": workflow_sha,
        "source_manifest_sha256": source_manifest_sha,
        "build_input_manifest_sha256": build_input_manifest_sha,
    }


def verify_git_source_provenance(source_repo_root, release_identity,
                                 provenance):
    """Recompute and compare the source side of a trusted build attestation."""
    expected = _source_provenance(provenance, require_trusted=True)
    observed = build_git_source_provenance(
        source_repo_root, release_identity,
        expected["build_workflow_identity"],
        build_workflow_run_id=expected["build_workflow_run_id"],
        build_workflow_sha=expected["build_workflow_sha"],
        build_workflow_run_attempt=expected["build_workflow_run_attempt"],
    )
    for key in (
            "provenance_class", "provenance_schema_version", "source_commit_sha",
            "source_tree_sha", "worktree_clean", "build_workflow_identity",
            "build_workflow_run_id", "build_workflow_run_attempt",
            "build_workflow_sha",
            "source_manifest_sha256", "build_input_manifest_sha256"):
        if expected.get(key) != observed.get(key):
            raise ValueError(
                "trusted candidate source provenance {} does not match checkout".format(
                    key
                )
            )
    return observed


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


def _attestation_payload(identity, payload):
    provenance = payload["source_provenance"]
    return {
        "attestation_version": CANDIDATE_BUILD_ATTESTATION_VERSION,
        "provenance_schema_version": provenance["provenance_schema_version"],
        "provenance_class": provenance["provenance_class"],
        "commit_sha": identity["commit_sha"],
        "build_id": identity["build_id"],
        "source_commit_sha": provenance["source_commit_sha"],
        "source_tree_sha": provenance["source_tree_sha"],
        "source_manifest_sha256": provenance["source_manifest_sha256"],
        "build_workflow_identity": provenance["build_workflow_identity"],
        "build_workflow_run_id": provenance["build_workflow_run_id"],
        "build_workflow_run_attempt": provenance["build_workflow_run_attempt"],
        "build_workflow_sha": provenance["build_workflow_sha"],
        "build_input_manifest_sha256": provenance["build_input_manifest_sha256"],
        "candidate_artifact_sha256": payload["artifact_sha256"],
        "release_identity_sha256": identity_manifest_sha256(identity),
    }


def _build_payload(candidate_root, identity, manifest_path, source_provenance,
                   attestation_path=None):
    candidate_root = _real(candidate_root)
    identity_snapshot = _identity_snapshot(identity)
    _verify_checkout_head(candidate_root, identity_snapshot["commit_sha"])
    manifest_path = _real(manifest_path)
    attestation_path = _real(attestation_path or os.path.join(
        candidate_root, CANDIDATE_BUILD_ATTESTATION_NAME
    ))
    receipt_path = _real(os.path.join(
        candidate_root, CANDIDATE_BUILD_RECEIPT_NAME
    ))
    if manifest_path == attestation_path or manifest_path == receipt_path or \
            attestation_path == receipt_path:
        raise ValueError(
            "candidate artifact metadata paths must be distinct"
        )
    for directory in ARTIFACT_DIRECTORIES:
        if not os.path.isdir(os.path.join(candidate_root, directory)):
            raise ValueError("candidate artifact is missing {}/".format(directory))
    entries = _inventory(
        candidate_root, manifest_path,
        excluded_paths=(attestation_path, receipt_path),
    )
    directory_hashes = {
        directory: _directory_hash(entries, directory)
        for directory in ARTIFACT_DIRECTORIES
    }
    artifact_sha = _canonical_hash(entries)
    provenance = _source_provenance(source_provenance)
    provenance["candidate_artifact_sha256"] = artifact_sha
    return {
        "artifact_manifest_version": CANDIDATE_ARTIFACT_MANIFEST_VERSION,
        "commit_sha": identity_snapshot["commit_sha"],
        "build_id": identity_snapshot["build_id"],
        "files": entries,
        "directory_sha256": directory_hashes,
        "reports_sha256": directory_hashes["reports"],
        "assets_sha256": directory_hashes["assets"],
        "registry_sha256": directory_hashes["registry"],
        "artifact_sha256": artifact_sha,
        "source_provenance": provenance,
        "provenance_schema_version": provenance["provenance_schema_version"],
        "source_commit_sha": provenance["source_commit_sha"],
        "source_tree_sha": provenance["source_tree_sha"],
        "build_workflow_identity": provenance["build_workflow_identity"],
        "build_workflow_run_id": provenance["build_workflow_run_id"],
        "build_workflow_run_attempt": provenance["build_workflow_run_attempt"],
        "build_workflow_sha": provenance["build_workflow_sha"],
        "source_manifest_sha256": provenance["source_manifest_sha256"],
        "build_input_manifest_sha256": provenance["build_input_manifest_sha256"],
        "candidate_artifact_sha256": artifact_sha,
        "attestation_path": _relative(candidate_root, attestation_path),
        "receipt_path": _relative(candidate_root, receipt_path),
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
    def build(cls, candidate_root, release_identity, output_path=None,
              source_provenance=None):
        candidate_root = _real(candidate_root)
        manifest_path = _real(output_path or os.path.join(
            candidate_root, CANDIDATE_ARTIFACT_MANIFEST_NAME
        ))
        identity = _identity_snapshot(release_identity)
        payload = _build_payload(
            candidate_root, identity, manifest_path, source_provenance
        )
        result = dict(payload)
        result.update(identity)
        result["release_identity"] = identity
        result["source_provenance"] = payload["source_provenance"]
        result["generated_at"] = utc_iso()
        result["manifest_path"] = _relative(candidate_root, manifest_path)
        attestation = _attestation_payload(identity, result)
        result["attestation_sha256"] = _canonical_hash(attestation)
        _write_json(
            _real(os.path.join(candidate_root, result["attestation_path"])),
            attestation,
        )
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
               manifest_path=None, require_trusted_provenance=False):
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
        declared_provenance = _source_provenance(
            manifest.get("source_provenance"), require_artifact_sha=True,
            require_trusted=require_trusted_provenance,
        )
        if declared_provenance.get("source_commit_sha", "").lower() != \
                expected_snapshot.get("commit_sha", "").lower():
            raise ValueError(
                "candidate artifact source_commit_sha does not match release identity"
            )
        attestation_relative = str(
            manifest.get("attestation_path") or CANDIDATE_BUILD_ATTESTATION_NAME
        )
        attestation_path = _real(os.path.join(candidate_root, attestation_relative))
        if not _inside(candidate_root, attestation_path) or \
                not os.path.isfile(attestation_path) or os.path.islink(attestation_path):
            raise ValueError("candidate build attestation is missing or invalid")
        observed = _build_payload(
            candidate_root, expected_snapshot, path, declared_provenance,
            attestation_path=attestation_path,
        )
        for key in (
                "commit_sha", "build_id", "files", "directory_sha256",
                "reports_sha256", "assets_sha256", "registry_sha256",
                "artifact_sha256", "source_provenance", "source_commit_sha",
                "source_tree_sha", "build_workflow_identity",
                "provenance_schema_version", "build_workflow_run_id",
                "build_workflow_run_attempt", "build_workflow_sha",
                "source_manifest_sha256",
                "build_input_manifest_sha256", "candidate_artifact_sha256",
                "attestation_path", "receipt_path"):
            if manifest.get(key) != observed.get(key):
                raise ValueError(
                    "candidate artifact manifest {} does not match candidate_root".format(key)
                )
        try:
            with open(attestation_path, "r", encoding="utf-8") as stream:
                attestation = json.load(stream)
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError("candidate build attestation is unreadable: {}".format(exc))
        expected_attestation = _attestation_payload(expected_snapshot, observed)
        if attestation != expected_attestation:
            raise ValueError("candidate build attestation does not match manifest")
        if manifest.get("attestation_sha256") != _canonical_hash(attestation):
            raise ValueError("candidate build attestation hash does not match manifest")
        return manifest
