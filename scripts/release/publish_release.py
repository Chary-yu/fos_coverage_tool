"""CLI for immutable report publication and atomic CURRENT switching."""

from __future__ import print_function

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.release_identity import load_release_manifest
from app.release_publication import ImmutableReleasePublisher


def _load(path):
    with open(path, "r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("release identity must be a JSON object")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(prog="publish_release.py")
    parser.add_argument("--publish-root", required=True)
    parser.add_argument("--source-root")
    parser.add_argument("--release-identity")
    parser.add_argument("--session-id")
    parser.add_argument("--api-contract-version", default="")
    parser.add_argument("--candidate-artifact-manifest", default="")
    parser.add_argument(
        "--source-repo-root", default="",
        help="clean Git checkout used to verify trusted Candidate provenance",
    )
    parser.add_argument(
        "--trusted-build-workflow-identity", default="",
        help="workflow identity trusted by the release operator",
    )
    parser.add_argument(
        "--trusted-build-workflow-sha", default="",
        help="exact workflow commit SHA trusted by the release operator",
    )
    parser.add_argument("--switch", action="store_true")
    parser.add_argument("--rollback-session")
    parser.add_argument("--validate-current", action="store_true")
    args = parser.parse_args(argv)
    publisher = ImmutableReleasePublisher(args.publish_root)
    if args.validate_current:
        result = publisher.validate_current()
    elif args.rollback_session:
        result = publisher.rollback(args.rollback_session)
    else:
        if not args.source_root or not args.release_identity or not args.session_id:
            parser.error(
                "--source-root, --release-identity and --session-id are required"
            )
        if not args.source_repo_root:
            parser.error(
                "--source-repo-root is required for normal trusted-ci-build publication"
            )
        if not args.trusted_build_workflow_identity or \
                not args.trusted_build_workflow_sha:
            parser.error(
                "--trusted-build-workflow-identity and "
                "--trusted-build-workflow-sha are required for normal publication"
            )
        identity = _load(args.release_identity)
        manifest = publisher.prepare(
            args.source_root, identity, args.session_id,
            api_contract_version=args.api_contract_version,
            candidate_artifact_manifest=args.candidate_artifact_manifest,
            source_repo_root=args.source_repo_root,
            trusted_build_workflow_identity=args.trusted_build_workflow_identity,
            trusted_build_workflow_sha=args.trusted_build_workflow_sha,
        )
        result = {
            "status": "PASSED",
            "release_validation_session_id": args.session_id,
            "release_root": publisher.release_path(args.session_id),
            "report_ids": manifest.get("report_ids") or [],
        }
        if args.switch:
            result["switch"] = publisher.switch_current(args.session_id)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("status") == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
