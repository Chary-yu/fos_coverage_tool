"""Build the production Release Candidate from the real Served Root.

The trusted browser/performance builder intentionally produces a deterministic
validation fixture.  This tool is the separate production artifact boundary:
it starts from the real FOS_V6R2 Served Root, refreshes only the repository-owned
browser assets, and creates a production-role Candidate manifest.  The
Publisher still requires the detached protected receipt and external
attestation before publication.

The implementation is Python 3.6-compatible and never normalizes report
content after the Candidate manifest is written.
"""

from __future__ import print_function

import argparse
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
from app.release_identity import generate_release_identity, save_release_manifest
from app.release_publication import (
    build_release_manifest, validate_production_candidate_content,
)


_PRODUCTION_ASSETS = (
    ("coverage_enhance.js", "web/assets/js/coverage_enhance.js"),
    ("coverage_enhance.css", "web/assets/css/coverage_enhance.css"),
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


def _refresh_canonical_assets(source_root, candidate_root):
    """Replace all served copies of the two release-owned browser assets."""
    refreshed = []
    for basename, source_relative in _PRODUCTION_ASSETS:
        source_path = os.path.join(_real(source_root), *source_relative.split("/"))
        if not os.path.isfile(source_path):
            raise ValueError("production source asset is missing: {}".format(source_relative))
        targets = [
            path for path in _walk_files(candidate_root)
            if os.path.basename(path) == basename
        ]
        if not targets:
            targets = [os.path.join(candidate_root, "assets", basename)]
        for target in sorted(set(targets)):
            shutil.copyfile(source_path, target)
            refreshed.append(os.path.relpath(target, candidate_root).replace(os.sep, "/"))
    return refreshed


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
        build_workflow_run_id, build_workflow_run_attempt, build_workflow_sha):
    """Create and validate one production-role Candidate artifact."""
    served_root = _real(served_root)
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
    _copy_served_root(served_root, candidate_root)
    refreshed_assets = _refresh_canonical_assets(source_repo_root, candidate_root)
    validate_production_candidate_content(candidate_root, PRODUCTION_PROJECT_NAME)
    _reject_validation_fixture(candidate_root)
    provenance = build_git_source_provenance(
        source_repo_root, identity, build_workflow_identity,
        build_workflow_run_id=build_workflow_run_id,
        build_workflow_run_attempt=build_workflow_run_attempt,
        build_workflow_sha=build_workflow_sha,
    )
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
    args = parser.parse_args(argv)
    try:
        result = build_production_candidate(
            args.served_root, args.source_repo_root,
            args.production_candidate_root, args.release_identity_output,
            args.build_workflow_identity, args.build_workflow_run_id,
            args.build_workflow_run_attempt, args.build_workflow_sha,
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise SystemExit("production Candidate build failed: {}".format(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
