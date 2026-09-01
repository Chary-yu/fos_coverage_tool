"""Sign a Candidate manifest after the external CI attestation is created."""

from __future__ import print_function

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.candidate_artifact import (
    CandidateArtifactManifest, verify_git_source_provenance,
    verify_trusted_build_policy,
)
from app.candidate_build_receipt import create_candidate_build_receipt
from app.release_identity import generate_release_identity


def _load(path, label):
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("{} is unreadable: {}".format(label, exc))
    if not isinstance(value, dict):
        raise ValueError("{} must be a JSON object".format(label))
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sign_candidate_build_receipt.py")
    parser.add_argument("--candidate-root", required=True)
    parser.add_argument("--release-identity", required=True)
    parser.add_argument("--source-repo-root", required=True)
    parser.add_argument("--build-workflow-identity", required=True)
    parser.add_argument("--build-workflow-run-id", required=True)
    parser.add_argument("--build-workflow-run-attempt", required=True)
    parser.add_argument("--build-workflow-sha", required=True)
    parser.add_argument("--attestation-bundle", required=True)
    parser.add_argument("--receipt-output", default="")
    args = parser.parse_args(argv)
    candidate_root = os.path.abspath(args.candidate_root)
    source_root = os.path.abspath(args.source_repo_root)
    try:
        identity = _load(args.release_identity, "release identity")
        observed_identity = generate_release_identity(
            source_root, build_provenance="release-build"
        )
        for key in (
                "version", "commit_sha", "build_id", "asset_hash",
                "schema_version", "asset_manifest_version", "asset_count",
                "asset_manifest_hash", "asset_manifest"):
            if identity.get(key) != observed_identity.get(key):
                raise ValueError(
                    "release identity {} does not match the clean source checkout".format(
                        key
                    )
                )
        manifest, manifest_path = CandidateArtifactManifest.load(candidate_root)
        verified = CandidateArtifactManifest.verify(
            candidate_root, identity,
            candidate_sha=identity.get("commit_sha"),
            manifest_path=manifest_path,
            require_trusted_provenance=True,
        )
        provenance = verified.get("source_provenance") or {}
        if str(provenance.get("build_workflow_identity") or "") != \
                str(args.build_workflow_identity).strip():
            raise ValueError("Candidate build workflow identity does not match receipt request")
        if str(provenance.get("build_workflow_run_id") or "") != \
                str(args.build_workflow_run_id).strip():
            raise ValueError("Candidate build workflow run ID does not match receipt request")
        if str(provenance.get("build_workflow_run_attempt") or "") != \
                str(args.build_workflow_run_attempt).strip():
            raise ValueError(
                "Candidate build workflow run attempt does not match receipt request"
            )
        if str(provenance.get("build_workflow_sha") or "").lower() != \
                str(args.build_workflow_sha).strip().lower():
            raise ValueError("Candidate build workflow SHA does not match receipt request")
        verify_trusted_build_policy(
            provenance, args.build_workflow_identity, args.build_workflow_sha
        )
        verify_git_source_provenance(source_root, identity, provenance)
        receipt = create_candidate_build_receipt(
            candidate_root, identity, manifest_path,
            output_path=args.receipt_output or "",
            attestation_bundle_path=args.attestation_bundle,
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps({
        "status": "PASSED",
        "receipt_path": os.path.abspath(args.receipt_output or os.path.join(
            candidate_root, "candidate_build_receipt.json"
        )),
        "receipt_version": receipt["receipt_version"],
        "candidate_manifest_sha256": receipt["payload"]["candidate_manifest_sha256"],
        "candidate_artifact_sha256": receipt["payload"]["candidate_artifact_sha256"],
        "attestation_bundle_sha256": receipt["payload"]["attestation_bundle_sha256"],
        "source_commit_sha": receipt["payload"]["source_commit_sha"],
        "source_tree_sha": receipt["payload"]["source_tree_sha"],
        "build_workflow_identity": receipt["payload"]["build_workflow_identity"],
        "build_workflow_run_id": receipt["payload"]["build_workflow_run_id"],
        "build_workflow_run_attempt": receipt["payload"][
            "build_workflow_run_attempt"
        ],
        "build_workflow_sha": receipt["payload"]["build_workflow_sha"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
