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
from app.candidate_artifact import (
    RELEASE_TRUST_MODE_OFFLINE_OPERATOR,
    RELEASE_TRUST_MODE_PROTECTED_BUILDER,
    RELEASE_TRUST_MODES,
)
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
    parser.add_argument(
        "--candidate-build-receipt", default="",
        help="detached protected receipt signed by the Candidate Build job",
    )
    parser.add_argument(
        "--candidate-build-attestation-bundle", default="",
        help="external GitHub artifact-attestation bundle for the Candidate manifest",
    )
    parser.add_argument(
        "--candidate-build-attestation-repository", default="",
        help="GitHub repository whose OIDC attestation is trusted",
    )
    parser.add_argument(
        "--candidate-build-attestation-workflow", default="",
        help="exact GitHub signer workflow path trusted for the Candidate build",
    )
    parser.add_argument(
        "--release-trust-mode", choices=RELEASE_TRUST_MODES,
        default=RELEASE_TRUST_MODE_PROTECTED_BUILDER,
    )
    parser.add_argument("--offline-operator-evidence", default="")
    parser.add_argument("--offline-operator-source-bundle", default="")
    parser.add_argument("--offline-operator-repository", default="")
    parser.add_argument("--production-host", default="")
    parser.add_argument("--production-baseline-sha", default="")
    parser.add_argument("--validation-session-id", default="")
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
                "--source-repo-root is required for Candidate source verification"
            )
        if args.release_trust_mode == RELEASE_TRUST_MODE_PROTECTED_BUILDER:
            if not args.trusted_build_workflow_identity or \
                    not args.trusted_build_workflow_sha:
                parser.error(
                    "--trusted-build-workflow-identity and "
                    "--trusted-build-workflow-sha are required for protected publication"
                )
            if not args.candidate_build_receipt or \
                    not args.candidate_build_attestation_bundle:
                parser.error(
                    "--candidate-build-receipt and "
                    "--candidate-build-attestation-bundle are required for protected publication"
                )
            if not args.candidate_build_attestation_repository or \
                    not args.candidate_build_attestation_workflow:
                parser.error(
                    "--candidate-build-attestation-repository and "
                    "--candidate-build-attestation-workflow are required for protected publication"
                )
        else:
            if not args.offline_operator_evidence or \
                    not args.offline_operator_source_bundle or \
                    not args.offline_operator_repository or \
                    not args.production_host or \
                    not args.production_baseline_sha or \
                    not args.validation_session_id:
                parser.error(
                    "offline publication requires --offline-operator-evidence, "
                    "--offline-operator-source-bundle, "
                    "--offline-operator-repository, --production-host, "
                    "--production-baseline-sha and --validation-session-id"
                )
        identity = _load(args.release_identity)
        manifest = publisher.prepare(
            args.source_root, identity, args.session_id,
            api_contract_version=args.api_contract_version,
            candidate_artifact_manifest=args.candidate_artifact_manifest,
            source_repo_root=args.source_repo_root,
            trusted_build_workflow_identity=args.trusted_build_workflow_identity,
            trusted_build_workflow_sha=args.trusted_build_workflow_sha,
            candidate_build_receipt=args.candidate_build_receipt,
            candidate_build_attestation_bundle=args.candidate_build_attestation_bundle,
            candidate_build_attestation_repository=args.candidate_build_attestation_repository,
            candidate_build_attestation_workflow=args.candidate_build_attestation_workflow,
            release_trust_mode=args.release_trust_mode,
            offline_operator_evidence=args.offline_operator_evidence,
            offline_operator_source_bundle=args.offline_operator_source_bundle,
            offline_operator_repository=args.offline_operator_repository,
            production_host=args.production_host,
            production_baseline_sha=args.production_baseline_sha,
            validation_session_id=args.validation_session_id,
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
