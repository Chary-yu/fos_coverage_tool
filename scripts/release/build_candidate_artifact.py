"""Build the repository-owned trusted Candidate artifact.

This lane intentionally does not accept a pre-populated Candidate directory.
It creates the Candidate from the exact clean checkout supplied by CI, then
normalizes and hashes the result.  The external GitHub artifact attestation
and protected receipt are added by the following signing step.
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
    CandidateArtifactManifest, build_git_source_provenance,
)
from app.release_identity import generate_release_identity, save_release_manifest
from app.release_publication import normalize_candidate_artifact


_SOURCE_ASSETS = (
    "web/assets/js/coverage_enhance.js",
    "web/assets/js/coverage_progress.js",
    "web/assets/js/incremental_coverage.js",
    "web/assets/js/incremental_developer_tasks.js",
    "web/assets/js/pending_snapshot.js",
    "web/assets/css/coverage_enhance.css",
)


def _write(path, value):
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(value)


def _write_json(path, value):
    _write(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _prepare_empty_root(path):
    path = os.path.abspath(path)
    if os.path.lexists(path):
        if os.path.islink(path) or not os.path.isdir(path):
            raise ValueError("candidate-root must be a directory created by the build lane")
        if os.listdir(path):
            raise ValueError(
                "candidate-root must be empty; pre-populated artifacts are not accepted"
            )
    else:
        os.makedirs(path)
    for directory in ("reports", "assets", "registry"):
        os.makedirs(os.path.join(path, directory))
    return path


def _ensure_parent(path):
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)


def _build_candidate_tree(source_root, candidate_root, identity):
    report_id = "candidate_build_{}".format(identity["commit_sha"][:12])
    asset_identity = "source-assets-{}".format(identity["asset_hash"][:16])
    for relative in _SOURCE_ASSETS:
        source_path = os.path.join(source_root, *relative.split("/"))
        if not os.path.isfile(source_path):
            raise ValueError("trusted Candidate build input is missing: {}".format(relative))
        shutil.copyfile(
            source_path,
            os.path.join(candidate_root, "assets", os.path.basename(relative)),
        )
    html = """<!doctype html>
<html><head>
<meta name="coverage-report-mode" content="VNEXT_ARTIFACT_READY">
<meta name="coverage-report-id" content="{report_id}">
<meta name="coverage-scan-id" content="1">
<meta name="coverage-repository-name" content="trusted-candidate-build">
<meta name="coverage-file-path" content="candidate-build/source.py">
<meta name="coverage-asset-identity" content="{asset_identity}">
<meta name="coverage-sidecar-schema" content="1">
</head><body data-source-commit="{commit_sha}">Trusted Candidate Build</body></html>
""".format(
        report_id=report_id,
        asset_identity=asset_identity,
        commit_sha=identity["commit_sha"],
    )
    _write(os.path.join(candidate_root, "reports", "index.html"), html)
    _write_json(
        os.path.join(candidate_root, "reports", ".source_cache", report_id, "meta.json"),
        {
            "schema_version": 1,
            "report_id": report_id,
            "source_commit_sha": identity["commit_sha"],
        },
    )
    _write_json(
        os.path.join(candidate_root, "registry", report_id + ".json"),
        {
            "report_id": report_id,
            "report_mode": "VNEXT_ARTIFACT_READY",
            "scan_id": 1,
            "report_root": "reports",
            "sidecar_schema": 1,
            "asset_identity": asset_identity,
            "repository_name": "trusted-candidate-build",
        },
    )


def _load_args(argv=None):
    parser = argparse.ArgumentParser(prog="build_candidate_artifact.py")
    parser.add_argument("--source-repo-root", required=True)
    parser.add_argument("--candidate-root", required=True)
    parser.add_argument("--release-identity-output", required=True)
    parser.add_argument("--build-workflow-identity", required=True)
    parser.add_argument("--build-workflow-run-id", required=True)
    parser.add_argument("--build-workflow-sha", required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = _load_args(argv)
    source_root = os.path.realpath(os.path.abspath(args.source_repo_root))
    candidate_root = _prepare_empty_root(args.candidate_root)
    identity_output = os.path.abspath(args.release_identity_output)
    try:
        identity = generate_release_identity(
            source_root, build_provenance="release-build"
        )
        _build_candidate_tree(source_root, candidate_root, identity)
        normalize_candidate_artifact(candidate_root)
        provenance = build_git_source_provenance(
            source_root, identity, args.build_workflow_identity,
            build_workflow_run_id=args.build_workflow_run_id,
            build_workflow_sha=args.build_workflow_sha,
        )
        manifest = CandidateArtifactManifest.build(
            candidate_root, identity, source_provenance=provenance
        )
        _ensure_parent(identity_output)
        save_release_manifest(identity_output, identity)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise SystemExit("trusted Candidate build failed: {}".format(exc))
    print(json.dumps({
        "status": "PASSED",
        "candidate_root": candidate_root,
        "release_identity": identity_output,
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
        "source_commit_sha": manifest["source_commit_sha"],
        "source_tree_sha": manifest["source_tree_sha"],
        "build_workflow_identity": manifest["build_workflow_identity"],
        "build_workflow_run_id": manifest["build_workflow_run_id"],
        "build_workflow_sha": manifest["build_workflow_sha"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
