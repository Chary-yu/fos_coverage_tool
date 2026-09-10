"""Build a production Candidate directly from a legacy Flat Root.

The first R8 migration cannot use ``publish_root/CURRENT`` as its build
source because CURRENT does not exist yet.  This entry point creates an
isolated deterministic adoption copy, binds it to the Flat source snapshot,
and builds the production Candidate from that copy.  The actual immutable
baseline bootstrap remains a Phase-D operation and must re-check the same
binding before switching CURRENT.
"""

from __future__ import print_function

import argparse
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.candidate_artifact import (
    CandidateArtifactManifest, OFFLINE_OPERATOR_PROVENANCE_CLASS,
    PRODUCTION_PROJECT_NAME, PRODUCTION_RELEASE_ARTIFACT_ROLE,
    RELEASE_TRUST_MODE_OFFLINE_OPERATOR, RELEASE_TRUST_MODE_PROTECTED_BUILDER,
    build_git_source_provenance, verify_offline_operator_trust,
)
from app.release_identity import generate_release_identity, save_release_manifest
from app.release_publication import (
    copy_production_application_bundle, validate_production_application_bundle,
    validate_production_candidate_content,
)
from app.time_utils import utc_iso
from scripts.release.build_production_candidate_artifact import (
    _copy_served_root, _prepare_empty_root, _refresh_release_assets,
    _release_asset_contract, _reject_validation_fixture, _real, _write_json,
)
from scripts.release.prepare_legacy_flat_adoption import (
    flat_source_binding, prepare_legacy_flat_adoption,
    verify_flat_source_binding,
)


def build_flat_production_candidate(
        flat_served_root, flat_release_identity_path, source_repo_root,
        production_candidate_root, release_identity_output,
        build_workflow_identity="", build_workflow_run_id="",
        build_workflow_run_attempt="", build_workflow_sha="",
        expected_previous_release_sha="", expected_source_binding=None,
        release_trust_mode=RELEASE_TRUST_MODE_PROTECTED_BUILDER,
        offline_operator_evidence_output="", offline_operator_source_bundle="",
        offline_operator_repository="", production_host="",
        production_baseline_sha="", validation_session_id="",
        flat_baseline_session_id=""):
    flat_served_root = _real(flat_served_root)
    flat_release_identity_path = _real(flat_release_identity_path)
    source_repo_root = _real(source_repo_root)
    if not expected_previous_release_sha:
        raise ValueError("expected_previous_release_sha is required")
    binding = flat_source_binding(
        flat_served_root, flat_release_identity_path,
        expected_previous_release_sha,
    )
    if expected_source_binding is not None and binding != expected_source_binding:
        raise ValueError("Flat source binding does not match the prepared Candidate input")
    identity = generate_release_identity(
        source_repo_root, build_provenance="release-build"
    )
    contract = _release_asset_contract(source_repo_root, identity)
    candidate_root = _prepare_empty_root(production_candidate_root)
    temporary_parent = tempfile.mkdtemp(prefix="coverage-flat-candidate-")
    staging_root = os.path.join(temporary_parent, "adoption")
    try:
        adoption = prepare_legacy_flat_adoption(
            flat_served_root, staging_root, flat_release_identity_path,
            expected_previous_release_sha,
        )
        observed = adoption.get("flat_source_binding") or {}
        if observed != binding:
            raise ValueError("Flat source binding changed during Candidate preparation")
        if expected_source_binding is not None and observed != expected_source_binding:
            raise ValueError("Flat source binding does not match the Candidate preparation")

        _copy_served_root(staging_root, candidate_root)
        application_evidence = copy_production_application_bundle(
            source_repo_root, candidate_root
        )
        _refresh_release_assets(source_repo_root, candidate_root, contract)
        validate_production_candidate_content(candidate_root, PRODUCTION_PROJECT_NAME)
        validate_production_application_bundle(candidate_root)
        _reject_validation_fixture(candidate_root)

        provenance_class = (
            OFFLINE_OPERATOR_PROVENANCE_CLASS
            if release_trust_mode == RELEASE_TRUST_MODE_OFFLINE_OPERATOR else ""
        )
        provenance = build_git_source_provenance(
            source_repo_root, identity,
            build_workflow_identity or (
                OFFLINE_OPERATOR_PROVENANCE_CLASS
                if provenance_class else ""
            ),
            build_workflow_run_id=build_workflow_run_id,
            build_workflow_run_attempt=build_workflow_run_attempt,
            build_workflow_sha=build_workflow_sha,
            provenance_class=provenance_class,
        )
        provenance.update({
            "served_root_path": flat_served_root,
            "served_root_realpath": flat_served_root,
            "previous_release_commit_sha": binding[
                "previous_release_commit_sha"
            ],
            # These legacy-compatible fields are required by the common
            # Candidate contract.  Their values are the deterministic Flat
            # binding, never a generated publication tree hash.
            "served_root_tree_sha256": binding["legacy_source_tree_sha256"],
            "served_root_identity_sha256": binding[
                "legacy_release_identity_sha256"
            ],
            "served_root_identity_file_sha256": binding[
                "legacy_release_identity_file_sha256"
            ],
            "previous_release_validation_session_id": str(
                flat_baseline_session_id or ""
            ),
            "flat_source_binding": binding,
        })
        manifest = CandidateArtifactManifest.build(
            candidate_root, identity, source_provenance=provenance,
            artifact_role=PRODUCTION_RELEASE_ARTIFACT_ROLE,
            production_publishable=True,
            project_name=PRODUCTION_PROJECT_NAME,
        )
        offline_path = ""
        if release_trust_mode == RELEASE_TRUST_MODE_OFFLINE_OPERATOR:
            required = {
                "offline_operator_evidence_output": offline_operator_evidence_output,
                "offline_operator_source_bundle": offline_operator_source_bundle,
                "offline_operator_repository": offline_operator_repository,
                "production_host": production_host,
                "production_baseline_sha": production_baseline_sha,
                "validation_session_id": validation_session_id,
            }
            missing = sorted(key for key, value in required.items()
                             if not str(value or "").strip())
            if missing:
                raise ValueError(
                    "offline operator build requires: {}".format(
                        ", ".join(missing)
                    )
                )
            bundle = _real(offline_operator_source_bundle)
            if not os.path.isfile(bundle) or os.path.islink(bundle):
                raise ValueError("offline operator source bundle is missing")
            offline_payload = {
                "schema_version": 1,
                "release_trust_mode": RELEASE_TRUST_MODE_OFFLINE_OPERATOR,
                "trust_class": "OFFLINE_OPERATOR",
                "repository": str(offline_operator_repository),
                "commit_sha": provenance["source_commit_sha"],
                "tree_sha": provenance["source_tree_sha"],
                "source_bundle_path": bundle,
                "source_bundle_sha256": _sha256(bundle),
                "candidate_tree_sha256": manifest["artifact_sha256"],
                "production_host": str(production_host),
                "production_baseline_sha": str(production_baseline_sha).lower(),
                "build_timestamp": utc_iso(),
                "validation_session_id": str(validation_session_id),
                "protected_builder": "SKIPPED_BY_OPERATOR",
                "offline_operator_source_integrity": "PASSED",
            }
            offline_path = os.path.abspath(str(offline_operator_evidence_output))
            _write_json(offline_path, offline_payload)
            verify_offline_operator_trust(
                candidate_root, identity, manifest, source_repo_root,
                evidence=offline_payload, source_bundle_path=bundle,
                expected_repository=offline_operator_repository,
                expected_production_host=production_host,
                expected_production_baseline_sha=production_baseline_sha,
                expected_validation_session_id=validation_session_id,
            )
        release_identity_output = os.path.abspath(str(release_identity_output))
        save_release_manifest(release_identity_output, identity)
        return {
            "status": "PASSED",
            "artifact_role": manifest["artifact_role"],
            "production_publishable": manifest["production_publishable"],
            "project_name": manifest["project_name"],
            "flat_source_binding": binding,
            "production_candidate_root": candidate_root,
            "release_identity": release_identity_output,
            "candidate_artifact_manifest": os.path.join(
                candidate_root, "candidate_artifact_manifest.json"
            ),
            "candidate_build_attestation": os.path.join(
                candidate_root, "candidate_build_attestation.json"
            ),
            "offline_operator_evidence": offline_path,
            "release_trust_mode": release_trust_mode,
            "commit_sha": manifest["commit_sha"],
            "build_id": manifest["build_id"],
            "artifact_sha256": manifest["artifact_sha256"],
            "reports_sha256": manifest["reports_sha256"],
            "assets_sha256": manifest["assets_sha256"],
            "registry_sha256": manifest["registry_sha256"],
            "application_sha256": application_evidence["application_sha256"],
            "application_file_count": application_evidence["file_count"],
            "source_binding_verified": True,
        }
    finally:
        shutil.rmtree(temporary_parent, ignore_errors=True)


def _sha256(path):
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="build_flat_production_candidate_artifact.py"
    )
    parser.add_argument("--flat-served-root", required=True)
    parser.add_argument("--flat-release-identity", required=True)
    parser.add_argument("--source-repo-root", required=True)
    parser.add_argument("--production-candidate-root", required=True)
    parser.add_argument("--release-identity-output", required=True)
    parser.add_argument("--expected-previous-release-sha", required=True)
    parser.add_argument("--expected-source-binding", default="")
    parser.add_argument("--build-workflow-identity", default="")
    parser.add_argument("--build-workflow-run-id", default="")
    parser.add_argument("--build-workflow-run-attempt", default="")
    parser.add_argument("--build-workflow-sha", default="")
    parser.add_argument("--release-trust-mode", default=RELEASE_TRUST_MODE_PROTECTED_BUILDER)
    parser.add_argument("--offline-operator-evidence-output", default="")
    parser.add_argument("--offline-operator-source-bundle", default="")
    parser.add_argument("--offline-operator-repository", default="")
    parser.add_argument("--production-host", default="")
    parser.add_argument("--production-baseline-sha", default="")
    parser.add_argument("--validation-session-id", default="")
    parser.add_argument("--flat-baseline-session-id", default="")
    args = parser.parse_args(argv)
    expected = None
    if args.expected_source_binding:
        with open(args.expected_source_binding, "r", encoding="utf-8") as stream:
            expected = json.load(stream)
    result = build_flat_production_candidate(
        args.flat_served_root, args.flat_release_identity,
        args.source_repo_root, args.production_candidate_root,
        args.release_identity_output,
        build_workflow_identity=args.build_workflow_identity,
        build_workflow_run_id=args.build_workflow_run_id,
        build_workflow_run_attempt=args.build_workflow_run_attempt,
        build_workflow_sha=args.build_workflow_sha,
        expected_previous_release_sha=args.expected_previous_release_sha,
        expected_source_binding=expected,
        release_trust_mode=args.release_trust_mode,
        offline_operator_evidence_output=args.offline_operator_evidence_output,
        offline_operator_source_bundle=args.offline_operator_source_bundle,
        offline_operator_repository=args.offline_operator_repository,
        production_host=args.production_host,
        production_baseline_sha=args.production_baseline_sha,
        validation_session_id=args.validation_session_id,
        flat_baseline_session_id=args.flat_baseline_session_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
