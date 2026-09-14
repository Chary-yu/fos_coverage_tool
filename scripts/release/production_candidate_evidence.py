"""Small, side-effect-free helpers for Production Candidate evidence.

The helpers in this module are shared by the protected Builder lane and the
offline/operator collector.  They deliberately do not read production state
or accept the receipt HMAC key; the key is consumed only by the protected
Builder job through :mod:`app.candidate_build_receipt`.
"""

from __future__ import print_function

import hashlib
import json
import os
import re
import stat


PRODUCTION_CANDIDATE_CALLER_JOB_NAME = (
    "Trusted Production Candidate Build (manual protected lane)"
)
PRODUCTION_CANDIDATE_CALLED_JOB_NAME = (
    "Build and attest exact production Candidate artifact"
)
PRODUCTION_CANDIDATE_JOB_NAMES = (
    PRODUCTION_CANDIDATE_CALLER_JOB_NAME,
    PRODUCTION_CANDIDATE_CALLED_JOB_NAME,
)
CANDIDATE_SOURCE_IDENTITY_JOB_NAME = "Candidate source identity gate"
CANDIDATE_SOURCE_GATE_JOB_NAME = "Candidate source gate (required source lanes)"
VALIDATION_CANDIDATE_JOB_NAME = (
    "Trusted Validation Candidate Build (manual protected lane)"
)
PRODUCTION_READY_JOB_NAME = "Production READY gate (manual external evidence)"
PRODUCTION_READY_FAILURE_STEP_NAME = (
    "Require all Production READY evidence lanes to pass"
)
CANDIDATE_ONLY_EXTERNAL_EVIDENCE_JOB_NAMES = (
    "Verified production backup rehearsal (MariaDB 5.5)",
    "Real Candidate browser evidence",
    "Cross-layer performance release evidence (browser artifact)",
)
CANDIDATE_ONLY_TEST_SUITE_JOB_NAMES = (
    "Test Suite (Python 3.10)",
    "Test Suite (Python 3.12)",
)
CANDIDATE_ONLY_GATE_FAILURE_STEP_NAME = (
    "Build fail-closed Gate A-F evidence matrix"
)
PROTECTED_RECEIPT_EVIDENCE_SCHEMA_VERSION = 1
PROTECTED_RECEIPT_EVIDENCE_TYPE = "protected_candidate_receipt_verification"
PROTECTED_RECEIPT_EVIDENCE_NAME = "protected_receipt_verification.json"
PROTECTED_RECEIPT_ATTESTATION_NAME = (
    "protected_receipt_verification_attestation.bundle.json"
)
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def _require_regular_file(path, label):
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


def sha256_file(path, label="evidence file"):
    path = _require_regular_file(path, label)
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path, label="JSON evidence"):
    path = _require_regular_file(path, label)
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError("{} is unreadable: {}".format(label, exc))
    if not isinstance(value, dict):
        raise ValueError("{} must be a JSON object".format(label))
    return value


def write_json(path, value):
    """Write one evidence object atomically without following a final symlink."""
    path = os.path.abspath(str(path))
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    if os.path.lexists(path):
        raise ValueError("evidence output already exists: {}".format(path))
    temporary = "{}.tmp-{}".format(path, os.getpid())
    try:
        with open(temporary, "x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            try:
                os.fsync(stream.fileno())
            except OSError:
                pass
        os.replace(temporary, path)
    except Exception:
        try:
            if os.path.lexists(temporary):
                os.unlink(temporary)
        except OSError:
            pass
        raise


def normalize_application_inventory_item(item):
    """Normalize a runtime inventory item to the Candidate manifest namespace."""
    if not isinstance(item, dict):
        raise ValueError("application inventory item is not an object")
    relative = str(item.get("path") or "")
    if not relative or relative.startswith("/") or relative.startswith("../") or \
            "/../" in relative or relative == "..":
        raise ValueError("application inventory path is unsafe: {}".format(relative))
    try:
        size = int(item.get("size"))
    except (TypeError, ValueError):
        raise ValueError("application inventory size is invalid")
    digest = str(item.get("sha256") or "")
    if size < 0 or not _SHA256.fullmatch(digest):
        raise ValueError("application inventory item digest/size is invalid")
    return {
        "path": "app/" + relative,
        "size": size,
        "sha256": digest.lower(),
    }


def normalize_application_inventory(entries):
    if not isinstance(entries, list):
        raise ValueError("application inventory is not a list")
    normalized = [normalize_application_inventory_item(item) for item in entries]
    paths = [item["path"] for item in normalized]
    if len(paths) != len(set(paths)):
        raise ValueError("application inventory contains duplicate paths")
    return sorted(normalized, key=lambda item: item["path"])


def compare_application_inventory(source_entries, candidate_entries,
                                  manifest_files):
    """Compare source/Candidate bytes and their ``app/`` manifest coverage."""
    source = normalize_application_inventory(source_entries)
    candidate = normalize_application_inventory(candidate_entries)
    if source != candidate:
        raise ValueError("Candidate application bundle bytes do not match exact Source")
    if not isinstance(manifest_files, list):
        raise ValueError("Candidate manifest files are not a list")
    manifest_application = []
    for item in manifest_files:
        if not isinstance(item, dict):
            raise ValueError("Candidate manifest file entry is not an object")
        path = str(item.get("path") or "")
        if path == "app" or path.startswith("app/"):
            manifest_application.append(normalize_manifest_application_item(item))
    if sorted(manifest_application, key=lambda item: item["path"]) != candidate:
        raise ValueError("Candidate manifest does not cover application bytes")
    return candidate


def normalize_manifest_application_item(item):
    """Normalize an existing manifest item without changing its ``app/`` path."""
    if not isinstance(item, dict):
        raise ValueError("Candidate manifest application item is not an object")
    path = str(item.get("path") or "")
    if not path.startswith("app/") or path == "app/":
        raise ValueError("Candidate manifest application path is invalid: {}".format(path))
    try:
        size = int(item.get("size"))
    except (TypeError, ValueError):
        raise ValueError("Candidate manifest application size is invalid")
    digest = str(item.get("sha256") or "")
    if size < 0 or not _SHA256.fullmatch(digest):
        raise ValueError("Candidate manifest application digest/size is invalid")
    return {"path": path, "size": size, "sha256": digest.lower()}


def select_unique_production_candidate_job(job_items):
    """Select exactly one successful caller/called Production Candidate job."""
    if not isinstance(job_items, list):
        raise ValueError("workflow jobs response is invalid")
    matches = [
        job for job in job_items
        if isinstance(job, dict)
        and job.get("name") in PRODUCTION_CANDIDATE_JOB_NAMES
    ]
    if len(matches) != 1:
        raise ValueError(
            "exact protected Production Candidate caller/called job is missing or ambiguous"
        )
    job = matches[0]
    if job.get("status") != "completed" or job.get("conclusion") != "success":
        raise ValueError("protected Production Candidate job did not pass")
    return job


def _select_unique_job_by_name(job_items, name):
    matches = [
        job for job in job_items
        if isinstance(job, dict) and job.get("name") == name
    ]
    if len(matches) != 1:
        raise ValueError("workflow job is missing or ambiguous: {}".format(name))
    return matches[0]


def _require_completed_job(job, name, conclusion):
    if job.get("status") != "completed" or job.get("conclusion") != conclusion:
        raise ValueError(
            "workflow job did not have expected {} conclusion: {}".format(
                conclusion, name
            )
        )


def _verify_expected_failure_step(job, job_name, expected_step_name):
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("{} has no verifiable step list".format(job_name))
    for step in steps:
        if not isinstance(step, dict) or step.get("status") != "completed":
            raise ValueError("{} has an incomplete workflow step".format(job_name))
    failures = [
        (index, step) for index, step in enumerate(steps)
        if step.get("conclusion") == "failure"
    ]
    if len(failures) != 1 or failures[0][1].get("name") != expected_step_name:
        raise ValueError(
            "{} failed outside the expected fail-closed step: {}".format(
                job_name, expected_step_name
            )
        )
    failure_index = failures[0][0]
    for step in steps[:failure_index]:
        if step.get("conclusion") != "success":
            raise ValueError(
                "{} has a non-success step before {}".format(
                    job_name, expected_step_name
                )
            )
    for step in steps[failure_index + 1:]:
        if step.get("conclusion") not in ("success", "skipped"):
            raise ValueError(
                "{} has an unexpected step after {}".format(
                    job_name, expected_step_name
                )
            )


def verify_candidate_only_workflow_jobs(job_items, overall_conclusion):
    """Verify a completed Candidate-only CI run without requiring green CI.

    A Candidate-only dispatch deliberately omits production backup, real
    browser, and cross-layer performance evidence.  The resulting
    Production READY gate (and, on some runners, the exact Gate A-F matrix
    step) may therefore fail closed.  Every other failure remains fatal.
    """
    if not isinstance(job_items, list):
        raise ValueError("workflow jobs response is invalid")
    if overall_conclusion not in ("success", "failure"):
        raise ValueError(
            "Candidate-only workflow conclusion is not success/failure: {}".format(
                overall_conclusion
            )
        )

    source_identity = _select_unique_job_by_name(
        job_items, CANDIDATE_SOURCE_IDENTITY_JOB_NAME
    )
    _require_completed_job(
        source_identity, CANDIDATE_SOURCE_IDENTITY_JOB_NAME, "success"
    )
    source_gate = _select_unique_job_by_name(
        job_items, CANDIDATE_SOURCE_GATE_JOB_NAME
    )
    _require_completed_job(source_gate, CANDIDATE_SOURCE_GATE_JOB_NAME, "success")

    production_candidate = select_unique_production_candidate_job(job_items)
    validation_candidate = _select_unique_job_by_name(
        job_items, VALIDATION_CANDIDATE_JOB_NAME
    )
    _require_completed_job(
        validation_candidate, VALIDATION_CANDIDATE_JOB_NAME, "skipped"
    )

    external_evidence = {}
    for name in CANDIDATE_ONLY_EXTERNAL_EVIDENCE_JOB_NAMES:
        job = _select_unique_job_by_name(job_items, name)
        _require_completed_job(job, name, "skipped")
        external_evidence[name] = job

    production_ready = _select_unique_job_by_name(
        job_items, PRODUCTION_READY_JOB_NAME
    )
    if production_ready.get("status") != "completed":
        raise ValueError("Production READY gate is not completed")
    ready_failure_allowed = False
    if production_ready.get("conclusion") == "failure":
        _verify_expected_failure_step(
            production_ready, PRODUCTION_READY_JOB_NAME,
            PRODUCTION_READY_FAILURE_STEP_NAME,
        )
        ready_failure_allowed = True
    elif production_ready.get("conclusion") != "success":
        raise ValueError("Production READY gate has an unexpected conclusion")
    else:
        raise ValueError(
            "Candidate-only Production READY gate unexpectedly passed with external evidence skipped"
        )

    test_suite_results = {}
    gate_failure_allowed = False
    for name in CANDIDATE_ONLY_TEST_SUITE_JOB_NAMES:
        job = _select_unique_job_by_name(job_items, name)
        if job.get("status") != "completed":
            raise ValueError("{} is not completed".format(name))
        conclusion = job.get("conclusion")
        if conclusion == "success":
            test_suite_results[name] = {
                "conclusion": conclusion,
                "gate_a_f_failure_allowed": False,
            }
        elif conclusion == "failure":
            _verify_expected_failure_step(
                job, name, CANDIDATE_ONLY_GATE_FAILURE_STEP_NAME
            )
            gate_failure_allowed = True
            test_suite_results[name] = {
                "conclusion": conclusion,
                "gate_a_f_failure_allowed": True,
            }
        else:
            raise ValueError(
                "{} has an unexpected conclusion: {}".format(name, conclusion)
            )

    known_names = {
        CANDIDATE_SOURCE_IDENTITY_JOB_NAME,
        CANDIDATE_SOURCE_GATE_JOB_NAME,
        VALIDATION_CANDIDATE_JOB_NAME,
        PRODUCTION_READY_JOB_NAME,
    }
    known_names.update(PRODUCTION_CANDIDATE_JOB_NAMES)
    known_names.update(CANDIDATE_ONLY_EXTERNAL_EVIDENCE_JOB_NAMES)
    known_names.update(CANDIDATE_ONLY_TEST_SUITE_JOB_NAMES)
    unexpected = []
    for job in job_items:
        if not isinstance(job, dict):
            raise ValueError("workflow jobs response contains a non-object job")
        name = job.get("name")
        if name in known_names:
            continue
        if job.get("status") != "completed":
            raise ValueError("unrelated workflow job is not completed: {}".format(name))
        if job.get("conclusion") != "success":
            unexpected.append((name, job.get("conclusion")))
    if unexpected:
        raise ValueError(
            "unexpected Candidate-only workflow job failure: {}".format(
                unexpected
            )
        )

    allowed_failure = ready_failure_allowed or gate_failure_allowed
    if overall_conclusion == "failure" and not allowed_failure:
        raise ValueError(
            "overall workflow failed without an expected Candidate-only fail-closed failure"
        )
    if overall_conclusion == "success" and allowed_failure:
        raise ValueError(
            "overall workflow is green despite an expected failed Candidate-only gate"
        )

    return {
        "status": "PASSED",
        "mode": "candidate_only",
        "overall_conclusion": overall_conclusion,
        "candidate_only_failure_allowed": allowed_failure,
        "source_identity_job": source_identity.get("name"),
        "source_gate_job": source_gate.get("name"),
        "production_candidate_job": production_candidate.get("name"),
        "validation_candidate_job_conclusion": validation_candidate.get(
            "conclusion"
        ),
        "external_evidence_job_conclusions": {
            name: job.get("conclusion")
            for name, job in external_evidence.items()
        },
        "production_ready_gate_conclusion": production_ready.get("conclusion"),
        "production_ready_failure_allowed": ready_failure_allowed,
        "test_suite_results": test_suite_results,
    }


def verify_protected_receipt_evidence(
        evidence_path, candidate_root, identity_path, manifest_path,
        receipt_path, candidate_attestation_bundle_path, source_sha,
        source_tree_sha, builder_sha, builder_identity, run_id, run_attempt,
        project_name, artifact_role, application_sha256="",
        application_file_count="", previous_release_sha="",
        served_root_tree_sha256="", served_root_identity_sha256=""):
    """Verify protected HMAC evidence against local Candidate bytes.

    This function intentionally never resolves or accepts the HMAC secret.  A
    valid, attested evidence JSON is the only HMAC result available to the
    ordinary collector.
    """
    evidence = load_json(evidence_path, "protected receipt verification evidence")
    identity = load_json(identity_path, "release identity")
    manifest = load_json(manifest_path, "Candidate artifact manifest")
    receipt = load_json(receipt_path, "Candidate build receipt")
    attestation_path = _require_regular_file(
        candidate_attestation_bundle_path,
        "Candidate external attestation bundle",
    )
    expected = {
        "evidence_schema_version": PROTECTED_RECEIPT_EVIDENCE_SCHEMA_VERSION,
        "evidence_type": PROTECTED_RECEIPT_EVIDENCE_TYPE,
        "status": "PASSED",
        "hmac_receipt_verification": "PASSED",
        "hmac_verified": True,
        "candidate_commit_sha": source_sha,
        "source_commit_sha": source_sha,
        "source_tree_sha": source_tree_sha,
        "build_workflow_identity": builder_identity,
        "build_workflow_sha": builder_sha,
        "build_workflow_run_id": str(run_id),
        "build_workflow_run_attempt": str(run_attempt),
        "artifact_role": artifact_role,
        "production_publishable": True,
        "project_name": project_name,
        "application_validation": "PASSED",
        "verification_runner_policy": "controlled-production-builder",
    }
    for key, value in expected.items():
        if evidence.get(key) != value:
            raise ValueError("protected receipt evidence field mismatch: {}".format(key))
    if identity.get("commit_sha") != source_sha:
        raise ValueError("protected evidence release identity Source SHA mismatch")
    if manifest.get("commit_sha") != source_sha:
        raise ValueError("protected evidence Candidate Source SHA mismatch")
    if manifest.get("artifact_role") != artifact_role or \
            manifest.get("production_publishable") is not True or \
            manifest.get("project_name") != project_name:
        raise ValueError("protected evidence Candidate publication role mismatch")
    provenance = manifest.get("source_provenance") or {}
    for key, value in (
            ("source_commit_sha", source_sha),
            ("source_tree_sha", source_tree_sha),
            ("build_workflow_identity", builder_identity),
            ("build_workflow_sha", builder_sha),
            ("build_workflow_run_id", str(run_id)),
            ("build_workflow_run_attempt", str(run_attempt))):
        if str(provenance.get(key) or "") != str(value):
            raise ValueError("protected evidence provenance mismatch: {}".format(key))
    if previous_release_sha and provenance.get("previous_release_commit_sha") != previous_release_sha:
        raise ValueError("protected evidence previous release SHA mismatch")
    if served_root_tree_sha256 and provenance.get("served_root_tree_sha256") != served_root_tree_sha256:
        raise ValueError("protected evidence Served Root tree hash mismatch")
    if served_root_identity_sha256 and provenance.get("served_root_identity_sha256") != served_root_identity_sha256:
        raise ValueError("protected evidence Served Root identity hash mismatch")
    if evidence.get("candidate_artifact_sha256") != manifest.get("artifact_sha256"):
        raise ValueError("protected evidence Candidate artifact SHA mismatch")
    if evidence.get("candidate_manifest_sha256") != sha256_file(
            manifest_path, "Candidate artifact manifest"):
        raise ValueError("protected evidence manifest digest mismatch")
    if evidence.get("candidate_receipt_sha256") != sha256_file(
            receipt_path, "Candidate build receipt"):
        raise ValueError("protected evidence receipt digest mismatch")
    if evidence.get("candidate_attestation_bundle_sha256") != sha256_file(
            attestation_path, "Candidate external attestation bundle"):
        raise ValueError("protected evidence attestation digest mismatch")
    if evidence.get("release_identity_sha256") != sha256_file(
            identity_path, "release identity"):
        raise ValueError("protected evidence release identity digest mismatch")
    if not isinstance(receipt.get("payload"), dict) or not receipt.get("signature"):
        raise ValueError("Candidate build receipt is structurally incomplete")
    if application_sha256 and evidence.get("application_sha256") != application_sha256:
        raise ValueError("protected evidence application SHA mismatch")
    if application_file_count and int(evidence.get("application_file_count") or -1) != int(application_file_count):
        raise ValueError("protected evidence application file count mismatch")
    for key, value in (
            ("previous_release_commit_sha", previous_release_sha),
            ("served_root_tree_sha256", served_root_tree_sha256),
            ("served_root_identity_sha256", served_root_identity_sha256)):
        if value and evidence.get(key) != value:
            raise ValueError("protected evidence field mismatch: {}".format(key))
    return evidence
