"""Build the production Release Candidate from the real Served Root.

The trusted browser/performance builder intentionally produces a deterministic
validation fixture.  This tool is the separate production artifact boundary:
it starts from the real FOS_V6R2 Served Root, refreshes the complete release
asset contract from the target identity, and creates a production-role Candidate
manifest.  The
Publisher still requires the detached protected receipt and external
attestation before publication.

The implementation is Python 3.6-compatible and never normalizes report
content after the Candidate manifest is written.
"""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import shutil
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.candidate_artifact import (
    CandidateArtifactManifest, PRODUCTION_PROJECT_NAME,
    PRODUCTION_RELEASE_ARTIFACT_ROLE, build_git_source_provenance,
)
from app.release_identity import (
    ASSET_MANIFEST_VERSION, DEFAULT_RELEASE_ASSET_RELATIVE_PATHS,
    build_asset_manifest,
    compute_asset_hash, generate_release_identity, is_valid_commit_sha,
    save_release_manifest,
)
from app.release_publication import (
    build_release_manifest, validate_production_candidate_content,
    validate_release_manifest,
)


_CONTROL_FILES = frozenset((
    "CURRENT", "candidate_artifact_manifest.json",
    "candidate_build_attestation.json", "candidate_build_receipt.json",
    "release_identity.json", "release_manifest.json", "report_manifest.json",
    "validated_publication_identity.json",
))


def _real(path):
    return os.path.realpath(os.path.abspath(str(path)))


def _inside(root, path):
    try:
        return os.path.commonpath((_real(root), _real(path))) == _real(root)
    except ValueError:
        return False


def _canonical_hash(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path, label):
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("{} is unreadable: {}".format(label, exc))
    if not isinstance(value, dict):
        raise ValueError("{} must be a JSON object".format(label))
    return value


def _safe_asset_path(root, relative):
    """Resolve a POSIX release-asset path without allowing traversal."""
    relative = str(relative or "").replace("\\", "/")
    if not relative or relative.startswith("/") or relative == ".":
        raise ValueError("release asset path is not relative: {}".format(relative))
    path = os.path.abspath(os.path.join(root, *relative.split("/")))
    if not _inside(root, path):
        raise ValueError("release asset path escapes root: {}".format(relative))
    return path


def _release_asset_contract(source_root, identity):
    """Return the target release's complete, verified source asset contract."""
    declared = identity.get("asset_manifest")
    if not isinstance(declared, list) or not declared:
        raise ValueError("target release identity has no asset_manifest")
    paths = []
    seen = set()
    for item in declared:
        if not isinstance(item, dict) or not item.get("path"):
            raise ValueError("target release identity asset_manifest is invalid")
        relative = str(item["path"]).replace("\\", "/")
        if relative in seen:
            raise ValueError("target release identity has duplicate asset: {}".format(relative))
        seen.add(relative)
        paths.append(_safe_asset_path(source_root, relative))

    missing_defaults = sorted(
        set(DEFAULT_RELEASE_ASSET_RELATIVE_PATHS) - seen
    )
    if missing_defaults:
        raise ValueError(
            "target release identity omits required release asset(s): {}".format(
                ", ".join(missing_defaults)
            )
        )
    observed = build_asset_manifest(paths, source_root)
    expected = sorted(declared, key=lambda item: item.get("path", ""))
    if observed != expected:
        raise ValueError(
            "target release identity asset_manifest does not match source checkout"
        )
    if int(identity.get("asset_count") or 0) != len(observed):
        raise ValueError("target release identity asset_count does not match asset_manifest")
    if int(identity.get("asset_manifest_version") or 0) != ASSET_MANIFEST_VERSION:
        raise ValueError(
            "target release identity asset_manifest_version is unsupported"
        )
    asset_hash = compute_asset_hash(paths, source_root)
    if str(identity.get("asset_hash") or "") != asset_hash or \
            str(identity.get("asset_manifest_hash") or "") != asset_hash:
        raise ValueError("target release identity asset hash does not match source checkout")

    by_basename = {}
    for item in observed:
        basename = os.path.basename(item["path"])
        fingerprint = (int(item["size"]), str(item["sha256"]))
        previous = by_basename.get(basename)
        if previous is not None and previous["fingerprint"] != fingerprint:
            raise ValueError(
                "target release identity has conflicting asset contract for {}".format(
                    basename
                )
            )
        by_basename[basename] = {
            "fingerprint": fingerprint,
            "source_path": _safe_asset_path(source_root, item["path"]),
        }
    return {
        "entries": observed,
        "by_path": {item["path"]: item for item in observed},
        "by_basename": by_basename,
    }


def _assert_no_symlinks(root):
    root = _real(root)
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        for name in sorted(dirnames + filenames):
            path = os.path.join(directory, name)
            if os.path.islink(path):
                raise ValueError("production Served Root may not contain symlinks: {}".format(path))


def _walk_files(root):
    result = []
    for directory, dirnames, filenames in os.walk(_real(root), followlinks=False):
        dirnames[:] = sorted(dirnames)
        for name in sorted(filenames):
            path = os.path.join(directory, name)
            if not os.path.isfile(path) or os.path.islink(path):
                raise ValueError("production artifact entry is not a regular file: {}".format(path))
            result.append(path)
    return result


def _served_root_tree_sha256(root):
    """Hash the complete pre-build Served Root, including control metadata."""
    entries = []
    for path in _walk_files(root):
        entries.append({
            "path": os.path.relpath(path, _real(root)).replace(os.sep, "/"),
            "size": int(os.path.getsize(path)),
            "sha256": _sha256(path),
        })
    return _canonical_hash(sorted(entries, key=lambda item: item["path"]))


def _served_root_binding(served_root):
    """Resolve and verify the immutable release selected by CURRENT.

    A production build must start from the actual ``publish_root/CURRENT``
    pointer, not an operator-selected directory that merely contains a
    plausible report.  The pointer target must be inside ``releases/`` and
    expose a release identity.  When an immutable release manifest is
    present, its complete Served Root validation is also performed here.
    """
    requested = os.path.abspath(str(served_root))
    if os.path.basename(os.path.normpath(requested)) != "CURRENT":
        raise ValueError("production served-root must be the publish_root/CURRENT path")
    if not os.path.islink(requested):
        raise ValueError("production served-root must be the immutable CURRENT symlink")
    publish_root = os.path.dirname(requested)
    resolved = _real(requested)
    releases_root = _real(os.path.join(publish_root, "releases"))
    if not _inside(releases_root, resolved) or resolved == releases_root:
        raise ValueError("CURRENT must resolve inside publish_root/releases")
    if not os.path.isdir(resolved):
        raise ValueError("CURRENT target is not a directory")
    _assert_no_symlinks(resolved)

    release_manifest_path = os.path.join(resolved, "release_manifest.json")
    identity_path = release_manifest_path
    release_manifest = None
    if os.path.isfile(release_manifest_path):
        release_manifest = _load_json(
            release_manifest_path, "CURRENT release manifest"
        )
        identity = release_manifest.get("release_identity")
        if not isinstance(identity, dict):
            identity = release_manifest
        checked = validate_release_manifest(resolved, release_manifest)
        if checked.get("status") != "PASSED":
            raise ValueError(
                "CURRENT release manifest is invalid: {}".format(
                    "; ".join(checked.get("violations") or [])
                )
            )
    else:
        identity_path = os.path.join(resolved, "release_identity.json")
        if not os.path.isfile(identity_path):
            raise ValueError(
                "CURRENT release identity evidence is missing: release_manifest.json"
            )
        identity = _load_json(identity_path, "CURRENT release identity")

    previous_sha = str(identity.get("commit_sha") or "").strip()
    if not is_valid_commit_sha(previous_sha):
        raise ValueError("CURRENT release identity has no exact previous release SHA")
    if release_manifest is not None and str(
            release_manifest.get("commit_sha") or "").lower() != previous_sha.lower():
        raise ValueError("CURRENT release manifest and identity commit SHA differ")
    return {
        "requested_path": requested,
        "realpath": resolved,
        "previous_release_sha": previous_sha,
        "served_root_tree_sha256": _served_root_tree_sha256(resolved),
        "current_identity_sha256": _canonical_hash(identity),
        "current_identity_file_sha256": _sha256(identity_path),
        "release_validation_session_id": str(
            (release_manifest or {}).get("release_validation_session_id") or ""
        ),
    }


def _prepare_empty_root(root):
    root = os.path.abspath(root)
    if os.path.lexists(root):
        if os.path.islink(root) or not os.path.isdir(root):
            raise ValueError("production-candidate-root must be a directory")
        if os.listdir(root):
            raise ValueError(
                "production-candidate-root must be empty; pre-populated artifacts are not accepted"
            )
    else:
        os.makedirs(root)
    return root


def _copy_served_root(served_root, candidate_root):
    """Copy the real payload while dropping stale publication control files."""
    served_root = _real(served_root)
    candidate_root = _real(candidate_root)
    _assert_no_symlinks(served_root)
    for name in sorted(os.listdir(served_root)):
        if name in _CONTROL_FILES:
            continue
        source = os.path.join(served_root, name)
        target = os.path.join(candidate_root, name)
        if os.path.isdir(source):
            shutil.copytree(source, target, symlinks=False)
        elif os.path.isfile(source):
            shutil.copy2(source, target)
        else:
            raise ValueError("unsupported Served Root entry: {}".format(source))
    for directory in ("reports", "assets", "registry"):
        if not os.path.isdir(os.path.join(candidate_root, directory)):
            raise ValueError("production Served Root is missing {}/".format(directory))


def _default_served_asset_target(candidate_root, basename):
    if basename.lower().endswith((".html", ".htm")):
        return os.path.join(candidate_root, basename)
    return os.path.join(candidate_root, "assets", basename)


def _copy_release_asset(source_path, target_path, candidate_root):
    target_path = _safe_asset_path(
        candidate_root,
        os.path.relpath(target_path, candidate_root).replace(os.sep, "/")
    )
    if os.path.lexists(target_path) and (
            os.path.islink(target_path) or not os.path.isfile(target_path)):
        raise ValueError("production release asset target is not a regular file: {}".format(
            target_path
        ))
    parent = os.path.dirname(target_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    shutil.copyfile(source_path, target_path)
    return target_path


def _verify_candidate_release_assets(candidate_root, contract):
    """Verify exact contract paths and every same-name Served alias."""
    _assert_no_symlinks(candidate_root)
    expected = contract["by_path"]
    exact_paths = [
        _safe_asset_path(candidate_root, relative)
        for relative in sorted(expected)
    ]
    observed = build_asset_manifest(exact_paths, candidate_root)
    if observed != [expected[path] for path in sorted(expected)]:
        raise ValueError(
            "production Candidate release asset path/size/sha256 contract failed"
        )
    for path in _walk_files(candidate_root):
        basename = os.path.basename(path)
        item = contract["by_basename"].get(basename)
        if item is None:
            continue
        actual = build_asset_manifest([path], candidate_root)[0]
        if (int(actual["size"]), actual["sha256"]) != item["fingerprint"]:
            raise ValueError(
                "conflicting Served asset copy for {}: {}".format(basename, path)
            )
    return [
        os.path.relpath(path, candidate_root).replace(os.sep, "/")
        for path in _walk_files(candidate_root)
        if os.path.basename(path) in contract["by_basename"]
    ]


def _refresh_release_assets(source_root, candidate_root, contract):
    """Refresh all release assets from the target identity, fail-closed.

    The release identity is the only source of truth.  Existing Served Root
    aliases are refreshed as a group, new target paths are materialized when
    a newly introduced asset (for example ``pending_snapshot.js``) did not
    exist in the previous release, and the final inventory is checked against
    every declared path plus every same-name alias.
    """
    refreshed = set()
    existing = {}
    for path in _walk_files(candidate_root):
        basename = os.path.basename(path)
        if basename not in contract["by_basename"]:
            continue
        existing.setdefault(basename, []).append(path)

    for basename, item in sorted(contract["by_basename"].items()):
        targets = sorted(existing.get(basename) or [])
        fingerprints = set()
        for target in targets:
            fingerprints.add((int(os.path.getsize(target)), _sha256(target)))
        if len(fingerprints) > 1:
            raise ValueError(
                "conflicting Served asset copies for {}".format(basename)
            )
        if not targets:
            targets = [_default_served_asset_target(candidate_root, basename)]
        for target in targets:
            copied = _copy_release_asset(item["source_path"], target, candidate_root)
            refreshed.add(os.path.relpath(copied, candidate_root).replace(os.sep, "/"))

    for relative, item in sorted(contract["by_path"].items()):
        copied = _copy_release_asset(
            _safe_asset_path(source_root, relative),
            _safe_asset_path(candidate_root, relative),
            candidate_root,
        )
        refreshed.add(os.path.relpath(copied, candidate_root).replace(os.sep, "/"))

    aliases = _verify_candidate_release_assets(candidate_root, contract)
    refreshed.update(aliases)
    return sorted(refreshed)


def _reject_validation_fixture(candidate_root):
    for path in _walk_files(os.path.join(candidate_root, "reports")):
        if not path.lower().endswith((".html", ".htm")):
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            text = stream.read()
        if "Coverage Candidate" in text or "coverage_candidate" in text:
            raise ValueError(
                "validation fixture content cannot be used as a production candidate: {}".format(
                    path
                )
            )


def build_production_candidate(
        served_root, source_repo_root, production_candidate_root,
        release_identity_output, build_workflow_identity,
        build_workflow_run_id, build_workflow_run_attempt, build_workflow_sha,
        expected_previous_release_sha="", expected_served_root_tree_sha256="",
        expected_current_identity_sha256=""):
    """Create and validate one production-role Candidate artifact."""
    served_root_binding = _served_root_binding(served_root)
    if expected_previous_release_sha and str(
            expected_previous_release_sha).lower() != served_root_binding[
                "previous_release_sha"
            ].lower():
        raise ValueError("CURRENT previous release SHA does not match the expected binding")
    if expected_served_root_tree_sha256 and str(
            expected_served_root_tree_sha256).lower() != served_root_binding[
                "served_root_tree_sha256"
            ].lower():
        raise ValueError("CURRENT Served Root tree hash does not match the expected binding")
    if expected_current_identity_sha256 and str(
            expected_current_identity_sha256).lower() != served_root_binding[
                "current_identity_sha256"
            ].lower():
        raise ValueError("CURRENT identity hash does not match the expected binding")
    served_root = served_root_binding["realpath"]
    source_repo_root = _real(source_repo_root)
    candidate_root = _real(production_candidate_root)
    if not os.path.isdir(served_root):
        raise ValueError("served-root is not a directory: {}".format(served_root))
    if _inside(served_root, candidate_root) or _inside(candidate_root, served_root):
        raise ValueError("production candidate root must be separate from Served Root")
    if _inside(source_repo_root, candidate_root) or _inside(candidate_root, source_repo_root):
        raise ValueError("production candidate root must be separate from source checkout")
    candidate_root = _prepare_empty_root(candidate_root)

    identity = generate_release_identity(
        source_repo_root, build_provenance="release-build"
    )
    asset_contract = _release_asset_contract(source_repo_root, identity)
    _copy_served_root(served_root, candidate_root)
    refreshed_assets = _refresh_release_assets(
        source_repo_root, candidate_root, asset_contract
    )
    validate_production_candidate_content(candidate_root, PRODUCTION_PROJECT_NAME)
    _reject_validation_fixture(candidate_root)
    provenance = build_git_source_provenance(
        source_repo_root, identity, build_workflow_identity,
        build_workflow_run_id=build_workflow_run_id,
        build_workflow_run_attempt=build_workflow_run_attempt,
        build_workflow_sha=build_workflow_sha,
    )
    provenance.update({
        "served_root_path": served_root_binding["requested_path"],
        "served_root_realpath": served_root_binding["realpath"],
        "previous_release_commit_sha": served_root_binding["previous_release_sha"],
        "served_root_tree_sha256": served_root_binding["served_root_tree_sha256"],
        "served_root_identity_sha256": served_root_binding["current_identity_sha256"],
        "served_root_identity_file_sha256": served_root_binding[
            "current_identity_file_sha256"
        ],
        "previous_release_validation_session_id": served_root_binding[
            "release_validation_session_id"
        ],
    })
    manifest = CandidateArtifactManifest.build(
        candidate_root, identity,
        source_provenance=provenance,
        artifact_role=PRODUCTION_RELEASE_ARTIFACT_ROLE,
        production_publishable=True,
        project_name=PRODUCTION_PROJECT_NAME,
    )
    # This is a pre-publication validation only.  It does not write a release
    # manifest into the Candidate and therefore cannot create a CURRENT.
    preflight = build_release_manifest(
        candidate_root, identity, "production-candidate-preflight",
        candidate_sha=identity["commit_sha"],
    )
    release_identity_output = os.path.abspath(release_identity_output)
    parent = os.path.dirname(release_identity_output)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    save_release_manifest(release_identity_output, identity)
    return {
        "status": "PASSED",
        "artifact_role": manifest["artifact_role"],
        "production_publishable": manifest["production_publishable"],
        "project_name": manifest["project_name"],
        "served_root": served_root,
        "served_root_binding": served_root_binding,
        "production_candidate_root": candidate_root,
        "release_identity": release_identity_output,
        "candidate_artifact_manifest": os.path.join(
            candidate_root, "candidate_artifact_manifest.json"
        ),
        "candidate_build_attestation": os.path.join(
            candidate_root, "candidate_build_attestation.json"
        ),
        "receipt_required": True,
        "commit_sha": manifest["commit_sha"],
        "build_id": manifest["build_id"],
        "artifact_sha256": manifest["artifact_sha256"],
        "reports_sha256": manifest["reports_sha256"],
        "assets_sha256": manifest["assets_sha256"],
        "registry_sha256": manifest["registry_sha256"],
        "refreshed_assets": refreshed_assets,
        "report_count": len(preflight.get("reports") or []),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="build_production_candidate_artifact.py"
    )
    parser.add_argument("--served-root", required=True)
    parser.add_argument("--source-repo-root", required=True)
    parser.add_argument("--production-candidate-root", required=True)
    parser.add_argument("--release-identity-output", required=True)
    parser.add_argument("--build-workflow-identity", required=True)
    parser.add_argument("--build-workflow-run-id", required=True)
    parser.add_argument("--build-workflow-run-attempt", required=True)
    parser.add_argument("--build-workflow-sha", required=True)
    parser.add_argument("--expected-previous-release-sha", default="")
    parser.add_argument("--expected-served-root-tree-sha256", default="")
    parser.add_argument("--expected-current-identity-sha256", default="")
    args = parser.parse_args(argv)
    try:
        result = build_production_candidate(
            args.served_root, args.source_repo_root,
            args.production_candidate_root, args.release_identity_output,
            args.build_workflow_identity, args.build_workflow_run_id,
            args.build_workflow_run_attempt, args.build_workflow_sha,
            expected_previous_release_sha=args.expected_previous_release_sha,
            expected_served_root_tree_sha256=args.expected_served_root_tree_sha256,
            expected_current_identity_sha256=args.expected_current_identity_sha256,
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise SystemExit("production Candidate build failed: {}".format(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
