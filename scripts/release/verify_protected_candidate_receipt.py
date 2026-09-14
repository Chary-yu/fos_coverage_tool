"""Verify a Production Candidate receipt inside the protected Builder lane.

The caller must run this script only in the protected GitHub job that owns
``COVERAGE_BUILD_PROVENANCE_HMAC_KEY``.  The output deliberately contains no
secret.  It is later attested as a separate immutable evidence subject for
the ordinary operator-side collector.
"""

from __future__ import print_function

import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.candidate_artifact import (  # noqa: E402
    CandidateArtifactManifest, PRODUCTION_PROJECT_NAME,
    PRODUCTION_RELEASE_ARTIFACT_ROLE,
)
from app.candidate_build_receipt import (  # noqa: E402
    ATTESTATION_RUNNER_POLICY_PRODUCTION_BUILDER,
    verify_candidate_build_receipt,
)
from app.release_publication import (  # noqa: E402
    validate_production_application_bundle,
)
from scripts.release.production_candidate_evidence import (  # noqa: E402
    PROTECTED_RECEIPT_EVIDENCE_SCHEMA_VERSION,
    PROTECTED_RECEIPT_EVIDENCE_TYPE,
    sha256_file,
    write_json,
)


def _load(path, label):
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError("{} is unreadable: {}".format(label, exc))
    if not isinstance(value, dict):
        raise ValueError("{} must be a JSON object".format(label))
    return value


def _git_value(root, *args):
    return subprocess.check_output(
        ["git"] + list(args), cwd=root, stderr=subprocess.STDOUT
    ).decode("ascii").strip()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="verify_protected_candidate_receipt.py"
    )
    parser.add_argument("--candidate-root", required=True)
    parser.add_argument("--release-identity", required=True)
    parser.add_argument("--source-repo-root", required=True)
    parser.add_argument("--candidate-manifest", required=True)
    parser.add_argument("--candidate-receipt", required=True)
    parser.add_argument("--candidate-attestation-bundle", required=True)
    parser.add_argument("--attestation-repository", required=True)
    parser.add_argument("--attestation-workflow", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-tree-sha", required=True)
    parser.add_argument("--builder-workflow-identity", required=True)
    parser.add_argument("--builder-workflow-sha", required=True)
    parser.add_argument("--workflow-run-id", required=True)
    parser.add_argument("--workflow-run-attempt", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    candidate_root = os.path.abspath(args.candidate_root)
    source_root = os.path.abspath(args.source_repo_root)
    try:
        if not os.environ.get("COVERAGE_BUILD_PROVENANCE_HMAC_KEY"):
            raise ValueError(
                "protected Builder environment did not provide the receipt verification key"
            )
        if _git_value(source_root, "rev-parse", "HEAD") != args.source_sha:
            raise ValueError("protected verification source checkout SHA mismatch")
        if _git_value(source_root, "rev-parse", "HEAD^{tree}") != args.source_tree_sha:
            raise ValueError("protected verification source tree SHA mismatch")
        if _git_value(source_root, "status", "--porcelain", "--untracked-files=all"):
            raise ValueError("protected verification source checkout is not clean")
        identity = _load(args.release_identity, "release identity")
        manifest = _load(args.candidate_manifest, "Candidate artifact manifest")
        verified_manifest = CandidateArtifactManifest.verify(
            candidate_root, identity, candidate_sha=args.source_sha,
            manifest_path=args.candidate_manifest,
            require_trusted_provenance=True,
            expected_artifact_role=PRODUCTION_RELEASE_ARTIFACT_ROLE,
            expected_project_name=PRODUCTION_PROJECT_NAME,
            require_production_publishable=True,
        )
        application = validate_production_application_bundle(candidate_root)
        receipt = verify_candidate_build_receipt(
            candidate_root, identity, verified_manifest,
            args.candidate_attestation_bundle,
            receipt_path=args.candidate_receipt,
            attestation_repository=args.attestation_repository,
            attestation_workflow=args.attestation_workflow,
            attestation_runner_policy=ATTESTATION_RUNNER_POLICY_PRODUCTION_BUILDER,
        )
        payload = receipt.get("payload") or {}
        for key, expected in (
                ("source_commit_sha", args.source_sha),
                ("source_tree_sha", args.source_tree_sha),
                ("build_workflow_identity", args.builder_workflow_identity),
                ("build_workflow_sha", args.builder_workflow_sha),
                ("build_workflow_run_id", args.workflow_run_id),
                ("build_workflow_run_attempt", args.workflow_run_attempt)):
            if str(payload.get(key) or "") != str(expected):
                raise ValueError("protected receipt field mismatch: {}".format(key))
        provenance = verified_manifest.get("source_provenance") or {}
        evidence = {
            "evidence_schema_version": PROTECTED_RECEIPT_EVIDENCE_SCHEMA_VERSION,
            "evidence_type": PROTECTED_RECEIPT_EVIDENCE_TYPE,
            "status": "PASSED",
            "hmac_receipt_verification": "PASSED",
            "hmac_verified": True,
            "candidate_commit_sha": verified_manifest["commit_sha"],
            "candidate_artifact_sha256": verified_manifest["artifact_sha256"],
            "candidate_manifest_sha256": sha256_file(
                args.candidate_manifest, "Candidate artifact manifest"
            ),
            "candidate_receipt_sha256": sha256_file(
                args.candidate_receipt, "Candidate build receipt"
            ),
            "candidate_attestation_bundle_sha256": sha256_file(
                args.candidate_attestation_bundle,
                "Candidate external attestation bundle",
            ),
            "release_identity_sha256": sha256_file(
                args.release_identity, "release identity"
            ),
            "source_commit_sha": args.source_sha,
            "source_tree_sha": args.source_tree_sha,
            "build_workflow_identity": args.builder_workflow_identity,
            "build_workflow_sha": args.builder_workflow_sha,
            "build_workflow_run_id": str(args.workflow_run_id),
            "build_workflow_run_attempt": str(args.workflow_run_attempt),
            "artifact_role": verified_manifest["artifact_role"],
            "production_publishable": verified_manifest["production_publishable"],
            "project_name": verified_manifest["project_name"],
            "application_validation": application["status"],
            "application_sha256": application["application_sha256"],
            "application_file_count": application["file_count"],
            "previous_release_commit_sha": provenance[
                "previous_release_commit_sha"
            ],
            "served_root_tree_sha256": provenance["served_root_tree_sha256"],
            "served_root_identity_sha256": provenance[
                "served_root_identity_sha256"
            ],
            "verification_runner_policy": ATTESTATION_RUNNER_POLICY_PRODUCTION_BUILDER,
            "verification_workflow": args.attestation_workflow,
            "verification_job_name": (
                "Build and attest exact production Candidate artifact"
            ),
            "verification_environment": "trusted-production-candidate-build",
        }
        write_json(args.output, evidence)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))
    print(json.dumps({
        "status": "PASSED",
        "protected_receipt_verification": os.path.abspath(args.output),
        "candidate_commit_sha": evidence["candidate_commit_sha"],
        "candidate_artifact_sha256": evidence["candidate_artifact_sha256"],
        "source_commit_sha": evidence["source_commit_sha"],
        "source_tree_sha": evidence["source_tree_sha"],
        "build_workflow_sha": evidence["build_workflow_sha"],
        "build_workflow_run_id": evidence["build_workflow_run_id"],
        "build_workflow_run_attempt": evidence["build_workflow_run_attempt"],
        "hmac_verified": True,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
