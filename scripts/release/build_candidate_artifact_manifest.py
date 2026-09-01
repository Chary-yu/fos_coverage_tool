"""Build the pre-publication CandidateArtifactManifest."""

from __future__ import print_function

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.candidate_artifact import (
    CANDIDATE_ARTIFACT_MANIFEST_NAME, CandidateArtifactManifest,
    build_git_source_provenance,
)
from app.release_identity import verify_release_identity


def _load(path):
    with open(path, "r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("release identity must be a JSON object")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(prog="build_candidate_artifact_manifest.py")
    parser.add_argument("--candidate-root", required=True)
    parser.add_argument("--release-identity", required=True)
    parser.add_argument("--output", default="")
    parser.add_argument(
        "--source-repo-root", required=True,
        help="exact clean checkout used to verify the supplied identity",
    )
    parser.add_argument(
        "--build-workflow-identity", required=True,
        help="immutable CI/build workflow identity for this artifact",
    )
    args = parser.parse_args(argv)
    candidate_root = os.path.abspath(args.candidate_root)
    identity = _load(args.release_identity)
    try:
        observed = verify_release_identity(
            os.path.abspath(args.source_repo_root), identity
        )
        identity = observed
        source_provenance = build_git_source_provenance(
            args.source_repo_root, identity, args.build_workflow_identity
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    output = args.output or os.path.join(
        candidate_root, CANDIDATE_ARTIFACT_MANIFEST_NAME
    )
    try:
        manifest = CandidateArtifactManifest.build(
            candidate_root, identity, output,
            source_provenance=source_provenance,
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps({
        "status": "PASSED",
        "manifest_path": os.path.abspath(output),
        "artifact_manifest_version": manifest["artifact_manifest_version"],
        "commit_sha": manifest["commit_sha"],
        "build_id": manifest["build_id"],
        "artifact_sha256": manifest["artifact_sha256"],
        "reports_sha256": manifest["reports_sha256"],
        "assets_sha256": manifest["assets_sha256"],
        "registry_sha256": manifest["registry_sha256"],
        "source_commit_sha": manifest["source_commit_sha"],
        "source_tree_sha": manifest["source_tree_sha"],
        "build_workflow_identity": manifest["build_workflow_identity"],
        "source_manifest_sha256": manifest["source_manifest_sha256"],
        "candidate_artifact_sha256": manifest["candidate_artifact_sha256"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
