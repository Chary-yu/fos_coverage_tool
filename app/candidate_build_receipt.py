"""Detached, protected provenance receipts for Candidate artifacts.

The JSON files produced by :mod:`app.candidate_artifact` are deliberately
self-describing, but a Candidate must not be allowed to establish its own
trust root.  This module adds a detached receipt signed by a secret that is
available only to the protected Candidate Build job and the release runtime.
The receipt binds the exact manifest bytes and an external GitHub artifact
attestation bundle to the source/workflow identity that the Publisher accepts.

The implementation uses only Python 3.6-compatible standard-library APIs.
The protected key is never stored in the receipt or Candidate directory.
"""

from __future__ import print_function

import hashlib
import hmac
import json
import os
import stat
import subprocess

from app.candidate_artifact import (
    CANDIDATE_BUILD_RECEIPT_NAME, CANDIDATE_BUILD_RECEIPT_VERSION,
    CandidateArtifactManifest,
)


CANDIDATE_BUILD_RECEIPT_TYPE = "protected-ci-build"
CANDIDATE_BUILD_RECEIPT_SIGNATURE_ALGORITHM = "HMAC-SHA256"
CANDIDATE_BUILD_RECEIPT_KEY_ENV = "COVERAGE_BUILD_PROVENANCE_HMAC_KEY"
_MINIMUM_KEY_LENGTH = 32


def _real(path):
    return os.path.realpath(os.path.abspath(str(path)))


def _inside(root, path):
    try:
        return os.path.commonpath((_real(root), _real(path))) == _real(root)
    except ValueError:
        return False


def _canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _canonical_hash(value):
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_regular_file(path, label):
    # Keep the non-resolved spelling while checking every path component. A
    # realpath before lstat would turn a symlink into its target and silently
    # defeat the evidence boundary.
    path = os.path.abspath(str(path))
    probe = path
    while True:
        if os.path.islink(probe):
            raise ValueError("{} must not be a symlink: {}".format(label, path))
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        file_stat = os.lstat(path)
    except OSError as exc:
        raise ValueError("{} is unavailable: {}".format(label, exc))
    if not stat.S_ISREG(file_stat.st_mode):
        raise ValueError("{} must be a regular file: {}".format(label, path))
    return path


def _resolve_key(key, label):
    value = str(key or os.environ.get(CANDIDATE_BUILD_RECEIPT_KEY_ENV) or "")
    if len(value) < _MINIMUM_KEY_LENGTH:
        raise ValueError(
            "{} must be supplied through {} and contain at least {} characters".format(
                label, CANDIDATE_BUILD_RECEIPT_KEY_ENV, _MINIMUM_KEY_LENGTH
            )
        )
    return value.encode("utf-8")


def _write_json(path, value):
    path = os.path.abspath(path)
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    temporary = "{}.tmp-{}".format(path, os.getpid())
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        try:
            os.fsync(stream.fileno())
        except OSError:
            pass
    os.replace(temporary, path)


def _load_json(path, label):
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("{} is unreadable: {}".format(label, exc))
    if not isinstance(value, dict):
        raise ValueError("{} must be a JSON object".format(label))
    return value


def _receipt_payload(candidate_root, manifest, manifest_path,
                     attestation_bundle_path):
    provenance = manifest.get("source_provenance") or {}
    return {
        "receipt_version": CANDIDATE_BUILD_RECEIPT_VERSION,
        "candidate_manifest_sha256": _sha256(manifest_path),
        "candidate_artifact_sha256": manifest.get("artifact_sha256"),
        "commit_sha": manifest.get("commit_sha"),
        "build_id": manifest.get("build_id"),
        "source_commit_sha": provenance.get("source_commit_sha"),
        "source_tree_sha": provenance.get("source_tree_sha"),
        "source_manifest_sha256": provenance.get("source_manifest_sha256"),
        "build_input_manifest_sha256": provenance.get(
            "build_input_manifest_sha256"
        ),
        "build_workflow_identity": provenance.get("build_workflow_identity"),
        "build_workflow_run_id": provenance.get("build_workflow_run_id"),
        "build_workflow_sha": provenance.get("build_workflow_sha"),
        "attestation_bundle_sha256": _sha256(attestation_bundle_path),
    }


def create_candidate_build_receipt(
        candidate_root, release_identity=None, manifest_path=None,
        output_path=None, attestation_bundle_path="", signing_key=""):
    """Create a protected receipt after an external attestation is available.

    The attestation bundle is deliberately an input to the signed payload.
    A receipt without that external subject proof cannot be created.
    """
    candidate_root = _real(candidate_root)
    manifest, resolved_manifest_path = CandidateArtifactManifest.load(
        candidate_root, manifest_path
    )
    identity = release_identity or manifest.get("release_identity") or manifest
    verified = CandidateArtifactManifest.verify(
        candidate_root, identity,
        candidate_sha=(identity or {}).get("commit_sha"),
        manifest_path=resolved_manifest_path,
        require_trusted_provenance=True,
    )
    bundle_path = _require_regular_file(
        attestation_bundle_path, "Candidate external attestation bundle"
    )
    receipt_path = os.path.abspath(output_path or os.path.join(
        candidate_root, CANDIDATE_BUILD_RECEIPT_NAME
    ))
    if not _inside(candidate_root, receipt_path):
        raise ValueError("Candidate build receipt must be inside candidate_root")
    if os.path.islink(receipt_path):
        raise ValueError("Candidate build receipt may not be a symlink")
    key = _resolve_key(signing_key, "Candidate build receipt signing key")
    payload = _receipt_payload(
        candidate_root, verified, resolved_manifest_path, bundle_path
    )
    payload_hash = _canonical_hash(payload)
    signature = hmac.new(key, _canonical_bytes(payload), hashlib.sha256).hexdigest()
    receipt = {
        "receipt_version": CANDIDATE_BUILD_RECEIPT_VERSION,
        "receipt_type": CANDIDATE_BUILD_RECEIPT_TYPE,
        "signature_algorithm": CANDIDATE_BUILD_RECEIPT_SIGNATURE_ALGORITHM,
        "payload": payload,
        "payload_sha256": payload_hash,
        "signature": signature,
    }
    _write_json(receipt_path, receipt)
    return receipt


def verify_candidate_build_receipt(
        candidate_root, release_identity, candidate_manifest,
        attestation_bundle_path, receipt_path="", verification_key="",
        attestation_repository="", attestation_workflow=""):
    """Verify the protected receipt against the current Candidate bytes."""
    candidate_root = _real(candidate_root)
    manifest_relative = str(
        (candidate_manifest or {}).get("manifest_path") or
        "candidate_artifact_manifest.json"
    )
    manifest_path = os.path.abspath(os.path.join(
        candidate_root, manifest_relative.replace("/", os.sep)
    ))
    if not _inside(candidate_root, manifest_path):
        raise ValueError("Candidate manifest path escapes candidate_root")
    manifest_path = _require_regular_file(
        manifest_path, "Candidate artifact manifest"
    )
    bundle_path = _require_regular_file(
        attestation_bundle_path, "Candidate external attestation bundle"
    )
    receipt_path = os.path.abspath(receipt_path or os.path.join(
        candidate_root,
        str((candidate_manifest or {}).get("receipt_path") or
            CANDIDATE_BUILD_RECEIPT_NAME).replace("/", os.sep),
    ))
    if not _inside(candidate_root, receipt_path):
        raise ValueError("Candidate build receipt must be inside candidate_root")
    receipt_path = _require_regular_file(
        receipt_path, "Candidate build receipt"
    )
    receipt = _load_json(receipt_path, "Candidate build receipt")
    if int(receipt.get("receipt_version") or 0) != \
            CANDIDATE_BUILD_RECEIPT_VERSION:
        raise ValueError("unsupported Candidate build receipt version")
    if receipt.get("receipt_type") != CANDIDATE_BUILD_RECEIPT_TYPE or \
            receipt.get("signature_algorithm") != \
            CANDIDATE_BUILD_RECEIPT_SIGNATURE_ALGORITHM:
        raise ValueError("Candidate build receipt trust type is invalid")
    payload = receipt.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Candidate build receipt payload is invalid")
    if receipt.get("payload_sha256") != _canonical_hash(payload):
        raise ValueError("Candidate build receipt payload hash is invalid")
    signature = str(receipt.get("signature") or "")
    if not signature:
        raise ValueError("Candidate build receipt signature is missing")
    key = _resolve_key(verification_key, "Candidate build receipt verification key")
    expected_signature = hmac.new(
        key, _canonical_bytes(payload), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        raise ValueError("Candidate build receipt signature is invalid")
    expected = _receipt_payload(
        candidate_root, candidate_manifest, manifest_path, bundle_path
    )
    if payload != expected:
        raise ValueError("Candidate build receipt does not match Candidate bytes")
    identity = dict(release_identity or {})
    if str(payload.get("commit_sha") or "").lower() != \
            str(identity.get("commit_sha") or "").lower():
        raise ValueError("Candidate build receipt commit does not match release identity")
    verify_github_artifact_attestation(
        manifest_path, bundle_path, attestation_repository,
        attestation_workflow, payload.get("source_commit_sha"),
        payload.get("build_workflow_sha"),
        payload.get("build_workflow_run_id"),
    )
    return receipt


def verify_github_artifact_attestation(
        subject_path, bundle_path, repository, workflow, source_commit_sha,
        workflow_sha, workflow_run_id, verifier="gh"):
    """Verify the Sigstore/GitHub attestation and its exact subject digest.

    ``gh attestation verify --bundle`` performs the cryptographic certificate,
    transparency-log and OIDC identity checks.  This wrapper adds the release
    policy arguments and refuses to treat a missing verifier as a pass.
    """
    repository = str(repository or "").strip()
    workflow = str(workflow or "").strip()
    source_commit_sha = str(source_commit_sha or "").strip()
    workflow_sha = str(workflow_sha or "").strip()
    workflow_run_id = str(workflow_run_id or "").strip()
    if not repository or not workflow or not workflow_run_id:
        raise ValueError(
            "GitHub artifact-attestation repository, signer workflow, and run ID are required"
        )
    subject_path = _require_regular_file(
        subject_path, "Candidate attestation subject"
    )
    bundle_path = _require_regular_file(
        bundle_path, "Candidate external attestation bundle"
    )
    command = [
        str(verifier), "attestation", "verify", subject_path,
        "--bundle", bundle_path,
        "--repo", repository,
        "--signer-workflow", workflow,
        "--source-digest", source_commit_sha,
        "--signer-digest", workflow_sha,
        "--predicate-type", "https://slsa.dev/provenance/v1",
        "--format", "json",
        "--deny-self-hosted-runners",
    ]
    try:
        output = subprocess.check_output(
            command, stderr=subprocess.STDOUT,
        ).decode("utf-8", "replace")
    except OSError as exc:
        raise ValueError(
            "GitHub artifact-attestation verifier is unavailable: {}".format(exc)
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.output or b"").decode("utf-8", "replace").strip()
        raise ValueError(
            "GitHub artifact attestation verification failed: {}".format(detail)
        )
    try:
        verified = json.loads(output)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "GitHub artifact-attestation verifier returned invalid JSON: {}".format(exc)
        )
    if not isinstance(verified, list) or not verified:
        raise ValueError("GitHub artifact-attestation verification returned no result")
    subject_sha = _sha256(subject_path)
    found_subject = False
    found_run = False
    for result in verified:
        if not isinstance(result, dict):
            continue
        verification = result.get("verificationResult") or {}
        statement = verification.get("statement") or {}
        for subject in statement.get("subject") or []:
            if not isinstance(subject, dict):
                continue
            digest = (subject.get("digest") or {}).get("sha256")
            if str(digest or "").lower() == subject_sha.lower():
                found_subject = True
                break
        if found_subject:
            predicate = statement.get("predicate") or {}
            if workflow_run_id in json.dumps(
                    predicate, ensure_ascii=False, sort_keys=True):
                found_run = True
            break
    if not found_subject:
        raise ValueError(
            "GitHub artifact attestation does not contain the Candidate manifest digest"
        )
    if not found_run:
        raise ValueError(
            "GitHub artifact attestation does not contain the Candidate build run ID"
        )
    return {
        "status": "PASSED",
        "subject_sha256": subject_sha,
        "repository": repository,
        "signer_workflow": workflow,
        "source_commit_sha": source_commit_sha,
        "signer_workflow_sha": workflow_sha,
        "workflow_run_id": workflow_run_id,
    }
