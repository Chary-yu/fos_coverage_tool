"""
Manifest-Driven Upgrade & Automated Rollback Orchestrator (Item 27 & 28)
Executes the production cutover workflow:
1. PRECHECK: Exact release/runtime/trust/deployment identity checks
2. CLASSIFY: Fail-closed Legacy/VNext and Flat/Immutable state classification
3. RESIDUE_GATE: Ownership-bound validation-process cleanup gate
4. BACKUP: Source-only full MySQL dump with SHA256 integrity check
5. BLUE_GREEN_MIGRATION: Disposable target schema/data migration and Ready Gate
6. CANDIDATE_RUNTIME: Candidate API, browser and performance validation
7. ROLLBACK_REHEARSAL: Verifiable old-release/old-database recovery evidence
8. CUTOVER: One immutable release preparation and atomic CURRENT switch
"""

import os
import sys
import time
import json
import hashlib
import math
import subprocess
import logging
import argparse
import re
import urllib.request
import shutil
import tempfile
import uuid
from typing import Dict, Any, List, Tuple, Optional

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.release_identity import is_valid_commit_sha, verify_release_identity
from app.release_publication import (
    ImmutableReleasePublisher, PRODUCTION_PROJECT_NAME,
    PRODUCTION_RELEASE_ARTIFACT_ROLE, current_served_root_binding,
    validate_production_application_bundle,
    validate_production_candidate_content,
)
from app.candidate_artifact import (
    CANDIDATE_ARTIFACT_MANIFEST_NAME, CandidateArtifactManifest,
    RELEASE_TRUST_MODE_OFFLINE_OPERATOR,
    RELEASE_TRUST_MODE_PROTECTED_BUILDER,
    RELEASE_TRUST_MODES,
    served_root_provenance_binding, verify_git_source_provenance,
    verify_offline_operator_trust, verify_trusted_build_policy,
)
from app.candidate_build_receipt import (
    ATTESTATION_RUNNER_POLICY_PRODUCTION_BUILDER,
    verify_candidate_build_receipt,
)
from scripts.upgrade.evidence_manifest import ProductionEvidenceManifest
from scripts.upgrade.schema_preflight import validate_ddl_file
from scripts.diagnostics.path_mapping_audit import audit_path_mappings, audit_lcov_paths
from scripts.diagnostics.security_scanner import scan_directory
from scripts.diagnostics.sidecar_registry_audit import audit_sidecar_and_registry
from scripts.diagnostics.served_root_identity import verify_http_served_root
from scripts.maintenance.mysql_backup import perform_database_backup
from scripts.upgrade.database_generation import (
    LEGACY, UNKNOWN, VNEXT, inspect_database_generation,
)
from scripts.upgrade.existing_vnext_upgrade import upgrade_existing_vnext
from scripts.upgrade.disposable_target import (
    DISPOSABLE_TARGET_MODES, EMPTY_NEW_TARGET, PRE_RESTORED_CONSISTENT_BACKUP,
    RESTORE_FROM_VERIFIED_BACKUP,
    _application_settings, create_disposable_target_from_backup,
    create_empty_disposable_target,
    probe_candidate_connection_access, validate_disposable_target_config,
)
from scripts.upgrade.migration_runner import (
    apply_schema, migrate_legacy, validate_migration_database_separation,
)
from scripts.upgrade.validation_residue import (
    BLOCKED as RESIDUE_BLOCKED, SAFE_TO_TEARDOWN,
    scan_validation_residue, teardown_validation_residue,
)
from scripts.upgrade.vfoswind_production_lifecycle import (
    ADAPTER_NAME as VFOSWIND_ADAPTER,
    VfoswindProductionLifecycle,
)
from scripts.release.current_adoption import (
    FLAT, IMMUTABLE_CURRENT, validate_current_or_plan_flat,
    bootstrap_flat_current,
)
from app.upgrade.lifecycle import UpgradeLifecycle
from scripts.upgrade.validation_session import (
    SESSION_SCHEMA_VERSION, ValidationSession,
)

logger = logging.getLogger(__name__)


def _path_is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([
            os.path.realpath(os.path.abspath(path)),
            os.path.realpath(os.path.abspath(root)),
        ]) == os.path.realpath(os.path.abspath(root))
    except (AttributeError, OSError, ValueError):
        return False


def _resolve_upgrade_path(repo_root: str, configured: Optional[str], field: str) -> str:
    """Resolve a required upgrade control path relative to the checkout."""
    if not configured:
        raise RuntimeError("upgrade.{} is required".format(field))
    value = str(configured)
    if not os.path.isabs(value):
        value = os.path.join(repo_root, value)
    return os.path.realpath(os.path.abspath(value))


def _resolve_upgrade_literal_path(repo_root: str, configured: Optional[str],
                                  field: str) -> str:
    """Resolve a path without dereferencing its final symlink components.

    The Nginx/static contract intentionally requires the literal
    ``publish_root/CURRENT/reports`` path.  Resolving it with ``realpath``
    would erase the CURRENT indirection and allow a stale release directory
    to be configured instead.
    """
    if not configured:
        raise RuntimeError("upgrade.{} is required".format(field))
    value = str(configured)
    if not os.path.isabs(value):
        value = os.path.join(repo_root, value)
    return os.path.normpath(os.path.abspath(value))


def _new_release_validation_session_id(commit_sha: str,
                                       configured: Optional[str] = None) -> str:
    """Return a unique validation attempt identity for one Candidate SHA."""
    explicit = str(configured or "").strip()
    if explicit:
        return explicit
    return "candidate-{}-{}".format(
        str(commit_sha or "").strip(), uuid.uuid4().hex[:16]
    )


def _resolve_attempt_path(repo_root: str, configured: Optional[str], field: str,
                          attempt_id: str) -> str:
    """Resolve an evidence path into an identity-specific attempt namespace."""
    if not configured:
        raise RuntimeError("upgrade.{} is required".format(field))
    raw = str(configured)
    if "{attempt_id}" in raw:
        raw = raw.replace("{attempt_id}", str(attempt_id))
    resolved = _resolve_upgrade_path(repo_root, raw, field)
    if "{attempt_id}" in str(configured):
        return resolved
    stem, extension = os.path.splitext(resolved)
    return os.path.realpath("{}.{}{}".format(stem, attempt_id, extension))


def _resolve_candidate_manifest_path(repo_root: str, candidate_root: str,
                                     configured: Optional[str]) -> str:
    """Resolve a Candidate manifest relative to its configured owner.

    Existing staging configurations express the manifest relative to the
    repository while some operators provide a path relative to candidate_root.
    Prefer the repository-relative path when it exists and let the manifest
    verifier enforce that the final path remains inside candidate_root.
    """
    if not configured:
        return os.path.join(candidate_root, CANDIDATE_ARTIFACT_MANIFEST_NAME)
    configured = str(configured)
    if os.path.isabs(configured):
        return os.path.realpath(configured)
    repo_relative = os.path.realpath(os.path.join(repo_root, configured))
    if os.path.isfile(repo_relative):
        return repo_relative
    return os.path.realpath(os.path.join(candidate_root, configured))


def validate_candidate_publication_preflight(
        repo_root: str, production_candidate_root: str,
        release_identity: Dict[str, Any],
        candidate_manifest_path: Optional[str], trusted_workflow_identity: str,
        trusted_workflow_sha: str, candidate_build_receipt: Optional[str] = "",
        candidate_build_attestation_bundle: Optional[str] = "",
        candidate_build_attestation_repository: Optional[str] = "",
        candidate_build_attestation_workflow: Optional[str] = "",
        release_trust_mode: str = RELEASE_TRUST_MODE_PROTECTED_BUILDER,
        offline_operator_evidence: Optional[str] = "",
        offline_operator_source_bundle: Optional[str] = "",
        offline_operator_repository: Optional[str] = "",
        production_host: Optional[str] = "",
        production_baseline_sha: Optional[str] = "",
        validation_session_id: Optional[str] = "") -> Dict[str, Any]:
    """Verify all candidate/source/trust inputs before maintenance begins."""
    release_trust_mode = str(
        release_trust_mode or RELEASE_TRUST_MODE_PROTECTED_BUILDER
    ).strip()
    if release_trust_mode not in RELEASE_TRUST_MODES:
        raise RuntimeError("unsupported release_trust_mode")
    trusted_workflow_identity = str(trusted_workflow_identity or "").strip()
    trusted_workflow_sha = str(trusted_workflow_sha or "").strip()
    if release_trust_mode == RELEASE_TRUST_MODE_PROTECTED_BUILDER:
        if not trusted_workflow_identity or not trusted_workflow_sha:
            raise RuntimeError(
                "trusted build workflow identity and SHA are required"
            )
        if "REPLACE_WITH" in trusted_workflow_sha.upper():
            raise RuntimeError(
                "production_candidate_builder_workflow_sha is still a placeholder"
            )
        if not is_valid_commit_sha(trusted_workflow_sha):
            raise RuntimeError(
                "production_candidate_builder_workflow_sha must be an exact commit SHA"
            )
        if not str(candidate_build_attestation_repository or "").strip() or \
                not str(candidate_build_attestation_workflow or "").strip():
            raise RuntimeError(
                "candidate build attestation repository and signer workflow are required"
            )
    production_candidate_root = os.path.realpath(
        os.path.abspath(production_candidate_root)
    )
    manifest_path = _resolve_candidate_manifest_path(
        os.path.realpath(os.path.abspath(repo_root)), production_candidate_root,
        candidate_manifest_path,
    )
    if not os.path.isfile(manifest_path):
        raise RuntimeError(
            "candidate artifact manifest is missing: {}".format(manifest_path)
        )
    manifest = CandidateArtifactManifest.verify(
        production_candidate_root, release_identity,
        candidate_sha=release_identity.get("commit_sha"),
        manifest_path=manifest_path,
        require_trusted_provenance=True,
        expected_artifact_role=PRODUCTION_RELEASE_ARTIFACT_ROLE,
        expected_project_name=PRODUCTION_PROJECT_NAME,
        require_production_publishable=True,
    )
    validate_production_candidate_content(
        production_candidate_root, PRODUCTION_PROJECT_NAME
    )
    if release_trust_mode == RELEASE_TRUST_MODE_PROTECTED_BUILDER:
        provenance = verify_trusted_build_policy(
            manifest.get("source_provenance") or {},
            trusted_workflow_identity, trusted_workflow_sha,
        )
        observed_source = verify_git_source_provenance(
            repo_root, release_identity, provenance,
        )
        receipt_path = _resolve_candidate_manifest_path(
            os.path.realpath(os.path.abspath(repo_root)), production_candidate_root,
            candidate_build_receipt or manifest.get("receipt_path") or
            "candidate_build_receipt.json",
        )
        if not candidate_build_attestation_bundle:
            raise RuntimeError(
                "candidate_build_attestation_bundle is required before maintenance"
            )
        bundle_path = str(candidate_build_attestation_bundle)
        if not os.path.isabs(bundle_path):
            bundle_path = os.path.join(
                os.path.realpath(os.path.abspath(repo_root)), bundle_path
            )
        bundle_path = os.path.realpath(os.path.abspath(bundle_path))
        verify_candidate_build_receipt(
            production_candidate_root, release_identity, manifest, bundle_path,
            receipt_path=receipt_path,
            attestation_repository=candidate_build_attestation_repository,
            attestation_workflow=candidate_build_attestation_workflow,
            attestation_runner_policy=ATTESTATION_RUNNER_POLICY_PRODUCTION_BUILDER,
        )
        trust_evidence = {
            "status": "PASSED",
            "release_trust_mode": RELEASE_TRUST_MODE_PROTECTED_BUILDER,
            "protected_builder": "PASSED",
            "trust_class": "PROTECTED_BUILDER",
        }
    else:
        provenance = manifest.get("source_provenance") or {}
        offline_repository = str(offline_operator_repository or "").strip()
        if not offline_repository:
            raise RuntimeError(
                "offline_operator_repository is required for offline trust"
            )
        evidence_path = str(offline_operator_evidence or "")
        if evidence_path and not os.path.isabs(evidence_path):
            evidence_path = os.path.join(
                os.path.realpath(os.path.abspath(repo_root)), evidence_path
            )
        source_bundle = str(offline_operator_source_bundle or "")
        if source_bundle and not os.path.isabs(source_bundle):
            source_bundle = os.path.join(
                os.path.realpath(os.path.abspath(repo_root)), source_bundle
            )
        observed_source = verify_offline_operator_trust(
            production_candidate_root, release_identity, manifest, repo_root,
            evidence_path=evidence_path,
            source_bundle_path=source_bundle,
            expected_repository=offline_repository,
            expected_production_host=production_host,
            expected_production_baseline_sha=production_baseline_sha,
            expected_validation_session_id=validation_session_id,
        )
        served_root_binding = served_root_provenance_binding(provenance)
        trust_evidence = dict(observed_source)
        receipt_path = ""
        bundle_path = ""
    served_root_binding = served_root_provenance_binding(
        manifest.get("source_provenance") or {}
    )
    return {
        "status": "PASSED",
        "production_candidate_root": production_candidate_root,
        "candidate_manifest_path": manifest_path,
        "candidate_artifact_sha256": manifest.get("artifact_sha256"),
        "artifact_role": manifest.get("artifact_role"),
        "production_publishable": manifest.get("production_publishable"),
        "project_name": manifest.get("project_name"),
        "source_provenance": dict(manifest.get("source_provenance") or {}),
        "source_commit_sha": observed_source.get("source_commit_sha"),
        "source_tree_sha": observed_source.get("source_tree_sha"),
        "previous_release_commit_sha": served_root_binding[
            "previous_release_commit_sha"
        ],
        "served_root_tree_sha256": served_root_binding[
            "served_root_tree_sha256"
        ],
        "served_root_identity_sha256": served_root_binding[
            "served_root_identity_sha256"
        ],
        "build_workflow_identity": trusted_workflow_identity,
        "build_workflow_sha": trusted_workflow_sha,
        "candidate_build_receipt": receipt_path,
        "candidate_build_attestation_bundle": bundle_path,
        "candidate_build_attestation_repository": str(
            candidate_build_attestation_repository or ""
        ).strip(),
        "candidate_build_attestation_workflow": str(
            candidate_build_attestation_workflow or ""
        ).strip(),
        "release_trust": trust_evidence,
        "release_trust_mode": release_trust_mode,
    }


def verify_production_candidate_served_root_binding(
        candidate_provenance: Dict[str, Any],
        current_binding: Dict[str, Any]) -> Dict[str, str]:
    """Join Candidate source CURRENT identity to the actual upgrade CURRENT."""
    candidate = served_root_provenance_binding(candidate_provenance)
    errors = []
    for field in (
            "previous_release_commit_sha", "served_root_tree_sha256",
            "served_root_identity_sha256"):
        actual = str(current_binding.get(field) or "").strip().lower()
        if candidate[field] != actual:
            errors.append(field)
    if errors:
        raise RuntimeError(
            "production Candidate Served Root binding {} does not match the "
            "CURRENT selected for this upgrade".format(", ".join(errors))
        )
    return candidate


def resolve_backup_root(repo_root: str, configured_root: Optional[str] = None) -> str:
    """Resolve a recoverable backup root outside the active deployment tree."""
    raw = configured_root or os.environ.get("COVERAGE_BACKUP_ROOT")
    if raw:
        candidate = str(raw)
        if not os.path.isabs(candidate):
            # Relative backup roots are deliberately relative to the deploy
            # parent, not the deploy tree itself.
            candidate = os.path.join(os.path.dirname(os.path.abspath(repo_root)), candidate)
    else:
        candidate = os.path.join(
            os.path.dirname(os.path.abspath(repo_root)),
            ".coverage-backups", os.path.basename(os.path.abspath(repo_root)),
        )
    candidate = os.path.realpath(os.path.abspath(candidate))
    if _path_is_within(candidate, repo_root):
        raise ValueError("backup root must be outside the active deployment root")
    return candidate


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_release_performance_artifact(path: str, payload: Dict[str, Any],
                                           target_revision: str,
                                           expected_session_id: str = "",
                                           expected_candidate_artifact_sha256: str = "",
                                           expected_served_root_sha256: str = "") -> List[str]:
    """Validate immutable release A/B evidence before the cutover can finish.

    The normal synthetic benchmark is intentionally not accepted here.  A
    release artifact must be produced by two isolated exact-revision runs and
    must retain the hashes of both source artifacts.
    """
    errors = []
    if not isinstance(payload, dict):
        return ["performance evidence is not a JSON object"]
    if payload.get("status") != "PASSED":
        errors.append("release performance A/B status is not PASSED")
    if payload.get("evidence_class") != "release_performance_ab":
        errors.append("performance evidence is not release_performance_ab")
    if payload.get("comparison_type") != "release_revision_ab":
        errors.append("performance evidence is not a release revision comparison")
    if payload.get("candidate_commit") != target_revision:
        errors.append("performance candidate_commit does not match target release")
    if expected_session_id and payload.get("release_validation_session_id") != expected_session_id:
        errors.append("performance release_validation_session_id does not match attempt")
    if expected_candidate_artifact_sha256 and payload.get("candidate_artifact_sha256") != expected_candidate_artifact_sha256:
        errors.append("performance candidate_artifact_sha256 does not match publication")
    if expected_served_root_sha256 and payload.get("served_root_sha256") != expected_served_root_sha256:
        errors.append("performance served_root_sha256 does not match publication")
    if not payload.get("baseline_commit") or payload.get("baseline_commit") == target_revision:
        errors.append("performance baseline_commit is missing or equals candidate")
    if not payload.get("workload_id") or not payload.get("workload_hash"):
        errors.append("performance workload identity is incomplete")
    if not isinstance(payload.get("environment_identity"), dict) or not payload.get("environment_identity"):
        errors.append("performance environment_identity is missing")
    if payload.get("exit_code") != 0:
        errors.append("PASSED performance evidence must have exit_code=0")
    if payload.get("synthetic"):
        errors.append("synthetic performance evidence cannot pass the release gate")

    for field in ("baseline_ms", "candidate_ms"):
        if not isinstance(payload.get(field), (int, float)) or not math.isfinite(float(payload.get(field))):
            errors.append("performance {} is missing or non-finite".format(field))
    for tier_name in ("Tier_A_1k", "Tier_B_10k", "Tier_C_50k", "Tier_D_100k"):
        tier = payload.get(tier_name)
        if not isinstance(tier, dict) or tier.get("status") != "PASSED":
            errors.append("performance tier {} is not PASSED".format(tier_name))
            continue
        for field in ("baseline_ms", "candidate_ms"):
            if not isinstance(tier.get(field), (int, float)) or not math.isfinite(float(tier.get(field))):
                errors.append("performance {}.{} is missing or non-finite".format(tier_name, field))

    virtual = payload.get("coverage_virtual_scroll_100k")
    if not isinstance(virtual, dict) or virtual.get("status") != "PASSED":
        errors.append("100k virtual-scroll release workload is not PASSED")
    elif not isinstance(virtual.get("candidate_elapsed_ms"), (int, float)):
        errors.append("100k virtual-scroll candidate elapsed time is missing")

    source_artifacts = payload.get("source_artifacts")
    source_payloads = {}
    if not isinstance(source_artifacts, dict):
        errors.append("source_artifacts are missing")
    else:
        for role in ("baseline", "candidate"):
            source = source_artifacts.get(role)
            source_path = source.get("path") if isinstance(source, dict) else ""
            source_sha = source.get("sha256") if isinstance(source, dict) else ""
            source_revision = source.get("revision") if isinstance(source, dict) else ""
            if not source_path or not os.path.isabs(str(source_path)) or not os.path.isfile(str(source_path)):
                errors.append("{} source performance artifact is missing".format(role))
                continue
            if not source_sha or _sha256_file(str(source_path)) != str(source_sha):
                errors.append("{} source performance artifact SHA256 mismatch".format(role))
            expected_revision = payload.get("baseline_commit") if role == "baseline" else payload.get("candidate_commit")
            if source_revision != expected_revision:
                errors.append("{} source performance artifact revision mismatch".format(role))
            try:
                with open(str(source_path), "r", encoding="utf-8") as source_stream:
                    source_payload = json.load(source_stream)
            except (OSError, ValueError, TypeError) as exc:
                errors.append("{} source performance artifact is unreadable: {}".format(role, exc))
                continue
            source_payloads[role] = source_payload
            if not isinstance(source_payload, dict) or source_payload.get("status") != "PASSED":
                errors.append("{} source performance artifact is not PASSED".format(role))
                continue
            if source_payload.get("evidence_class") != "release_performance_revision" or \
                    source_payload.get("comparison_type") != "single_revision":
                errors.append("{} source performance artifact is not an independent revision run".format(role))
            if source_payload.get("revision") != expected_revision:
                errors.append("{} source performance payload revision mismatch".format(role))
            if source_payload.get("workload_id") != payload.get("workload_id") or \
                    source_payload.get("workload_hash") != payload.get("workload_hash"):
                errors.append("{} source performance workload identity mismatch".format(role))
            if source_payload.get("environment_identity") != payload.get("environment_identity"):
                errors.append("{} source performance environment identity mismatch".format(role))

    # Re-check the combined timings against the hashed source JSON. A modified
    # summary must not be able to retain valid source hashes while changing the
    # displayed measurements or PASS status.
    for role, source_payload in source_payloads.items():
        source_tiers = source_payload.get("tiers") or source_payload
        for tier_name in ("Tier_A_1k", "Tier_B_10k", "Tier_C_50k", "Tier_D_100k"):
            source_tier = source_tiers.get(tier_name) if isinstance(source_tiers, dict) else None
            combined_tier = payload.get(tier_name) or {}
            if not isinstance(source_tier, dict) or not isinstance(combined_tier, dict):
                continue
            measured = source_tier.get("measured_ms")
            expected_field = "baseline_ms" if role == "baseline" else "candidate_ms"
            observed = combined_tier.get(expected_field)
            if not isinstance(measured, (int, float)) or not isinstance(observed, (int, float)) or \
                    round(float(measured), 3) != round(float(observed), 3):
                errors.append("{} source timing mismatch for {}".format(role, tier_name))
        source_virtual = source_payload.get("coverage_virtual_scroll_100k") or {}
        combined_virtual = payload.get("coverage_virtual_scroll_100k") or {}
        expected_virtual_field = "baseline_elapsed_ms" if role == "baseline" else "candidate_elapsed_ms"
        if isinstance(source_virtual, dict) and isinstance(combined_virtual, dict):
            measured = source_virtual.get("elapsed_ms")
            observed = combined_virtual.get(expected_virtual_field)
            if not isinstance(measured, (int, float)) or not isinstance(observed, (int, float)) or \
                    round(float(measured), 3) != round(float(observed), 3):
                errors.append("{} source timing mismatch for 100k virtual-scroll".format(role))

    if not path or not os.path.isfile(path):
        errors.append("release performance artifact path is missing")
    return errors


def _normalize_evidence_url(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def _validate_candidate_browser_evidence(path: str, payload: Dict[str, Any],
                                         identity: Dict[str, Any],
                                         expected_url: str,
                                         expected_session_id: str = "",
                                         expected_candidate_artifact_sha256: str = "",
                                         expected_served_root_sha256: str = "") -> Tuple[List[str], Dict[str, Any]]:
    """Validate the external real-Candidate browser evidence envelope."""
    errors = []
    if not isinstance(payload, dict):
        return ["Candidate browser evidence is not a JSON object"], {}
    if payload.get("status") != "PASSED":
        errors.append("Candidate browser evidence status is not PASSED")
    if payload.get("evidence_class") != "real_http_chromium_browser":
        errors.append("Candidate browser evidence is not real_http_chromium_browser")
    if payload.get("release_eligible") is not True:
        errors.append("Candidate browser evidence is not release eligible")
    if payload.get("synthetic") is not False:
        errors.append("synthetic Candidate browser evidence is forbidden")
    if expected_session_id and payload.get("release_validation_session_id") != expected_session_id:
        errors.append("Candidate browser release_validation_session_id does not match attempt")
    if expected_candidate_artifact_sha256 and payload.get("candidate_artifact_sha256") != expected_candidate_artifact_sha256:
        errors.append("Candidate browser candidate_artifact_sha256 does not match publication")
    if expected_served_root_sha256 and payload.get("served_root_sha256") != expected_served_root_sha256:
        errors.append("Candidate browser served_root_sha256 does not match publication")
    target_revision = identity.get("commit_sha")
    if payload.get("candidate_revision") != target_revision:
        errors.append("Candidate browser revision does not match target release")
    candidate_url = payload.get("candidate_url") or payload.get("page_url")
    if not candidate_url:
        errors.append("Candidate browser evidence URL is missing")
    elif not _normalize_evidence_url(candidate_url).startswith(("http:", "https:")):
        errors.append("Candidate browser evidence URL is not HTTP(S)")
    if not expected_url:
        errors.append("upgrade.candidate_browser_url is required")
    elif _normalize_evidence_url(candidate_url) != _normalize_evidence_url(expected_url):
        errors.append("Candidate browser URL does not match configured candidate_browser_url")

    served_identity = payload.get("release_identity") or {}
    if not isinstance(served_identity, dict):
        errors.append("Candidate browser served release identity is missing")
        served_identity = {}
    for key in (
            "version", "commit_sha", "build_id", "asset_hash", "schema_version",
            "asset_manifest_version", "asset_count", "asset_manifest_hash",
            "asset_manifest"):
        if identity.get(key) not in (None, "") and served_identity.get(key) != identity.get(key):
            errors.append("Candidate browser served release identity mismatch: {}".format(key))

    functional = payload.get("browser_functional") or {}
    workload = payload.get("coverage_virtual_scroll_100k") or {}
    environment = workload.get("environment_identity") or {}
    if functional.get("status") != "PASSED":
        errors.append("Candidate browser functional HTTP check is not PASSED")
    if workload.get("status") != "PASSED":
        errors.append("Candidate browser workload is not PASSED")
    if environment.get("browser_name") != "chromium":
        errors.append("Candidate browser evidence does not identify Chromium")

    artifact_path = payload.get("artifact_path") or payload.get("report_artifact_path")
    artifact_sha = payload.get("artifact_sha256") or payload.get("report_artifact_sha256")
    if not artifact_path or not os.path.isabs(str(artifact_path)) or \
            not os.path.isfile(str(artifact_path)):
        errors.append("Candidate browser workload artifact is missing")
        artifact_path = ""
        artifact_sha = ""
    else:
        actual_sha = _sha256_file(str(artifact_path))
        if not artifact_sha or str(artifact_sha) != actual_sha:
            errors.append("Candidate browser workload artifact SHA256 mismatch")
            artifact_sha = actual_sha

    normalized = {
        "status": "PASSED" if not errors else "FAILED",
        "evidence_class": "real_candidate_browser",
        "source_evidence_class": payload.get("evidence_class", ""),
        "release_validation_session_id": payload.get("release_validation_session_id", ""),
        "candidate_artifact_sha256": payload.get("candidate_artifact_sha256", ""),
        "served_root_sha256": payload.get("served_root_sha256", ""),
        "candidate_url": candidate_url or "",
        "expected_commit_sha": target_revision,
        "served_release_identity": served_identity,
        "browser_artifact_path": artifact_path,
        "browser_artifact_sha256": artifact_sha,
        "real_http": payload.get("evidence_class") == "real_http_chromium_browser" and \
            _normalize_evidence_url(candidate_url).startswith(("http:", "https:")),
        "chromium": environment.get("browser_name") == "chromium",
        "synthetic": payload.get("synthetic"),
        "release_eligible": payload.get("release_eligible"),
        "workload_id": workload.get("workload_id", ""),
        "workload_status": workload.get("status", ""),
        "violations": errors,
    }
    return errors, normalized


def connect_live_database(db_config: Dict[str, Any]):
    """Create the live DB-API connection required by a non-dry-run upgrade."""
    try:
        import pymysql
    except ImportError as exc:
        raise RuntimeError("PyMySQL is required for a live upgrade connection") from exc
    cfg = dict(db_config or {})
    return pymysql.connect(
        host=cfg.get("host", "127.0.0.1"),
        port=int(cfg.get("port", 3306)),
        user=cfg.get("user", "root"),
        password=str(cfg.get("password", "")),
        database=cfg.get("database", "coverage_tool"),
        charset=cfg.get("charset", "utf8mb4"),
        autocommit=False,
        connect_timeout=float(cfg.get("connect_timeout", 5.0)),
        cursorclass=pymysql.cursors.DictCursor,
    )

class UpgradeOrchestrator:
    def __init__(self, repo_root: Optional[str] = None,
                 backup_root: Optional[str] = None):
        self.repo_root = os.path.realpath(repo_root or _REPO_ROOT)
        self.manifest = ProductionEvidenceManifest(self.repo_root)
        self.backup_dir = resolve_backup_root(self.repo_root, backup_root)
        self.publisher = None
        self.production_candidate_root = ""
        self.validation_candidate_root = ""
        self.publish_root = ""
        self.served_root_path = ""
        self.validation_session = None
        self.validation_session_manifest_path = ""
        self.validation_teardown_evidence_path = ""
        self.serving_session_manifest_path = ""
        self.serving_teardown_evidence_path = ""
        self.serving_state_path = ""
        self.serving_session_id = ""
        self.release_validation_session_id = ""
        self.previous_published_session_id = ""
        self.candidate_browser_evidence_path = ""
        self.rollback_evidence_path = ""
        self.performance_evidence_path = ""
        self.candidate_preflight = {}
        self._candidate_artifact_sha256 = ""
        self._served_root_sha256 = ""
        self._target_identity = {}
        self._upgrade_mode = ""
        self._runtime_config = {}
        self._validation_teardown_result = None
        self.logs: List[str] = []
        self._publication_switched = False
        self._previous_release_identity = {}
        self._release_trust_mode = RELEASE_TRUST_MODE_PROTECTED_BUILDER
        self._deployment_layout = ""
        self._current_adoption_plan = None
        self._database_generation = {}
        self._target_database_generation = {}
        self._target_db_config = {}
        self._target_preparation = {}
        self._target_cleanup_done = False
        self._production_lifecycle_adapter = None
        self._production_runtime_bound = False
        self._production_release_bindings_changed = False

    def _configure_backup_root(self, configured_root: Optional[str]):
        resolved = resolve_backup_root(self.repo_root, configured_root)
        if resolved == self.backup_dir:
            return
        if self._publication_switched:
            raise RuntimeError("backup root cannot change after file cutover")
        self.backup_dir = resolved

    def _configure_release_controls(self, upgrade_config: Dict[str, Any],
                                    identity: Dict[str, Any],
                                    previous_release: Dict[str, Any],
                                    runtime_config: Dict[str, Any]):
        """Bind the upgrade to immutable publication and an owned session.

        The active checkout is not a publication target.  The only mutable
        release operation permitted here is an atomic ``CURRENT`` pointer
        switch performed by ``ImmutableReleasePublisher``.
        """
        if upgrade_config.get("candidate_root"):
            raise RuntimeError(
                "upgrade.candidate_root is retired; use production_candidate_root"
            )
        if upgrade_config.get("trusted_build_workflow_identity") or \
                upgrade_config.get("trusted_build_workflow_sha"):
            raise RuntimeError(
                "upgrade.trusted_build_workflow_* is retired; use the explicit "
                "validation/production Candidate builder policies"
            )
        production_candidate_root = _resolve_upgrade_path(
            self.repo_root, upgrade_config.get("production_candidate_root"),
            "production_candidate_root"
        )
        validation_candidate_root = ""
        if upgrade_config.get("validation_candidate_root"):
            validation_candidate_root = _resolve_upgrade_path(
                self.repo_root, upgrade_config.get("validation_candidate_root"),
                "validation_candidate_root"
            )
        publish_root = _resolve_upgrade_path(
            self.repo_root, upgrade_config.get("publish_root"), "publish_root"
        )
        if _path_is_within(publish_root, self.repo_root):
            raise RuntimeError("upgrade.publish_root must be outside the active deployment root")
        if _path_is_within(production_candidate_root, self.repo_root):
            raise RuntimeError(
                "upgrade.production_candidate_root must be outside the active deployment root"
            )
        if _path_is_within(publish_root, production_candidate_root) or \
                _path_is_within(production_candidate_root, publish_root):
            raise RuntimeError(
                "upgrade.publish_root and production_candidate_root must be separate"
            )
        if validation_candidate_root and (
                _path_is_within(validation_candidate_root, self.repo_root) or
                _path_is_within(publish_root, validation_candidate_root) or
                _path_is_within(validation_candidate_root, publish_root) or
                _path_is_within(validation_candidate_root, production_candidate_root) or
                _path_is_within(production_candidate_root, validation_candidate_root)):
            raise RuntimeError(
                "validation_candidate_root must be separate from production and publication roots"
            )
        trusted_workflow_identity = str(
            upgrade_config.get("production_candidate_builder_workflow_identity") or ""
        ).strip()
        trusted_workflow_sha = str(
            upgrade_config.get("production_candidate_builder_workflow_sha") or ""
        ).strip()
        self._release_trust_mode = str(
            upgrade_config.get("release_trust_mode") or
            RELEASE_TRUST_MODE_PROTECTED_BUILDER
        ).strip()
        self.candidate_preflight = validate_candidate_publication_preflight(
            self.repo_root, production_candidate_root, identity,
            upgrade_config.get("production_candidate_artifact_manifest", ""),
            trusted_workflow_identity, trusted_workflow_sha,
            upgrade_config.get("production_candidate_build_receipt", ""),
            upgrade_config.get("production_candidate_attestation_bundle", ""),
            upgrade_config.get("production_candidate_attestation_repository", ""),
            upgrade_config.get("production_candidate_attestation_workflow", ""),
            release_trust_mode=self._release_trust_mode,
            offline_operator_evidence=upgrade_config.get(
                "offline_operator_evidence", ""
            ),
            offline_operator_source_bundle=upgrade_config.get(
                "offline_operator_source_bundle", ""
            ),
            offline_operator_repository=upgrade_config.get(
                "offline_operator_repository", ""
            ),
            production_host=upgrade_config.get("production_host", ""),
            production_baseline_sha=upgrade_config.get("production_baseline_sha") or
            previous_release.get("commit_sha", ""),
            validation_session_id=upgrade_config.get(
                "release_validation_session_id"
            ) or upgrade_config.get("validation_session_id", ""),
        )

        self.production_candidate_root = production_candidate_root
        self.validation_candidate_root = validation_candidate_root
        self.publish_root = publish_root
        self.served_root_path = _resolve_upgrade_literal_path(
            self.repo_root, upgrade_config.get("served_root_path"),
            "served_root_path",
        )
        expected_served_root_path = os.path.normpath(os.path.abspath(
            os.path.join(publish_root, "CURRENT", "reports")
        ))
        if self.served_root_path != expected_served_root_path:
            raise RuntimeError(
                "upgrade.served_root_path must be the literal publish_root/CURRENT/reports path"
            )
        flat_root = upgrade_config.get("flat_served_root") or upgrade_config.get(
            "legacy_flat_served_root", ""
        )
        if flat_root and not os.path.isabs(str(flat_root)):
            flat_root = os.path.join(self.repo_root, str(flat_root))
        flat_identity = upgrade_config.get("flat_release_identity_path") or \
            upgrade_config.get("legacy_flat_release_identity_path", "")
        if flat_identity and not os.path.isabs(str(flat_identity)):
            flat_identity = os.path.join(self.repo_root, str(flat_identity))
        deployment = validate_current_or_plan_flat(
            publish_root, flat_root, flat_identity,
            previous_release.get("commit_sha", ""),
        )
        self._deployment_layout = deployment.get("deployment_layout", "")
        self.publisher = ImmutableReleasePublisher(
            publish_root, create_root=self._deployment_layout != FLAT
        )
        if self._deployment_layout == IMMUTABLE_CURRENT:
            current_validation = self.publisher.validate_current(persist=False)
            if current_validation.get("status") != "PASSED":
                raise RuntimeError(
                    "CURRENT release failed validation: {}".format(
                        "; ".join(current_validation.get("violations") or [])
                    )
                )
            self.candidate_preflight["current_validation"] = current_validation
            current = deployment.get("current") or {}
            current_binding = deployment.get("current_binding") or {}
            candidate_binding = verify_production_candidate_served_root_binding(
                self.candidate_preflight.get("source_provenance") or {},
                current_binding,
            )
            if str(current.get("commit_sha") or "").lower() != \
                    current_binding["previous_release_commit_sha"]:
                raise RuntimeError(
                    "CURRENT commit does not match the CURRENT binding"
                )
            if current.get("commit_sha") != previous_release.get("commit_sha"):
                raise RuntimeError(
                    "CURRENT release does not match previous_release commit_sha"
                )
            self.candidate_preflight["candidate_served_root_binding"] = candidate_binding
            self.candidate_preflight["current_served_root_binding"] = current_binding
            self.previous_published_session_id = self.publisher.current_session_id()
            if not self.previous_published_session_id:
                raise RuntimeError("CURRENT release-validation session identity is missing")
        elif self._deployment_layout == FLAT:
            if not upgrade_config.get("flat_current_adoption_on_cutover"):
                raise RuntimeError(
                    "Flat deployment requires explicit flat_current_adoption_on_cutover"
                )
            if not str(upgrade_config.get("flat_baseline_session_id") or "").strip():
                raise RuntimeError(
                    "Flat deployment requires flat_baseline_session_id"
                )
            self._current_adoption_plan = deployment
            self.candidate_preflight["deployment_layout"] = FLAT
            self.candidate_preflight["flat_current_adoption"] = deployment
            # The baseline session is created by the explicit adoption step at
            # cutover.  It is intentionally not fabricated as CURRENT here.
            self.previous_published_session_id = str(
                upgrade_config.get("flat_baseline_session_id") or ""
            ).strip()
        else:
            raise RuntimeError("deployment layout is unknown")

        session_id = _new_release_validation_session_id(
            identity.get("commit_sha"),
            upgrade_config.get("release_validation_session_id") or
            upgrade_config.get("validation_session_id"),
        ).strip()
        try:
            self.publisher.release_path(session_id)
        except ValueError as exc:
            raise RuntimeError("validation session id is invalid: {}".format(exc))
        self.release_validation_session_id = session_id
        self._previous_release_identity = dict(previous_release)

        self.validation_session_manifest_path = _resolve_attempt_path(
            self.repo_root,
            upgrade_config.get("validation_session_manifest"),
            "validation_session_manifest",
            session_id,
        )
        configured_teardown = upgrade_config.get("validation_teardown_evidence_path")
        teardown_config = configured_teardown or (
            self.validation_session_manifest_path + ".teardown.json"
        )
        self.validation_teardown_evidence_path = _resolve_attempt_path(
            self.repo_root,
            teardown_config,
            "validation_teardown_evidence_path",
            session_id,
        )
        if self.validation_teardown_evidence_path == self.validation_session_manifest_path:
            raise RuntimeError(
                "validation teardown evidence must not overwrite the session manifest"
            )

        self.candidate_browser_evidence_path = _resolve_attempt_path(
            self.repo_root,
            upgrade_config.get("candidate_browser_evidence_path") or os.environ.get(
                "COVERAGE_CANDIDATE_BROWSER_EVIDENCE", ""
            ),
            "candidate_browser_evidence_path",
            session_id,
        )
        self.rollback_evidence_path = _resolve_attempt_path(
            self.repo_root,
            upgrade_config.get("rollback_evidence_path") or os.environ.get(
                "COVERAGE_ROLLBACK_EVIDENCE", ""
            ),
            "rollback_evidence_path",
            session_id,
        )
        self.performance_evidence_path = _resolve_attempt_path(
            self.repo_root,
            upgrade_config.get("performance_evidence_path") or os.environ.get(
                "COVERAGE_RELEASE_PERFORMANCE_AB", ""
            ),
            "performance_evidence_path",
            session_id,
        )

        if os.path.isfile(self.validation_session_manifest_path):
            session = ValidationSession.load(self.validation_session_manifest_path)
            if session.data.get("session_id") != session_id:
                raise RuntimeError("validation session id does not match manifest")
        else:
            server = runtime_config.get("server") or {}
            configured_ports = upgrade_config.get("validation_ports")
            if configured_ports is None:
                configured_ports = [server.get("port")]
            if isinstance(configured_ports, (str, int)):
                configured_ports = [configured_ports]
            try:
                ports = sorted(set(int(port) for port in configured_ports if port))
            except (TypeError, ValueError):
                raise RuntimeError("upgrade.validation_ports must contain integers")
            if not ports:
                raise RuntimeError(
                    "upgrade.validation_ports must identify at least one owned port"
                )
            session = ValidationSession.create(
                self.validation_session_manifest_path,
                session_id,
                candidate_sha=identity.get("commit_sha"),
                baseline_sha=previous_release.get("commit_sha"),
                ports=ports,
                evidence_paths=[self.validation_teardown_evidence_path],
            )

        if int(session.data.get("schema_version") or 0) != SESSION_SCHEMA_VERSION:
            raise RuntimeError("validation session schema version is unsupported")
        if not session.data.get("session_id"):
            raise RuntimeError("validation session id is missing")
        if session.data.get("candidate_sha") != identity.get("commit_sha"):
            raise RuntimeError("validation session candidate SHA mismatches target release")
        if session.data.get("baseline_sha") != previous_release.get("commit_sha"):
            raise RuntimeError("validation session baseline SHA mismatches previous release")
        if session.data.get("teardown_status") == "PASSED":
            raise RuntimeError("validation session has already been torn down")
        if not session.data.get("ports"):
            raise RuntimeError("validation session has no owned validation ports")
        self.validation_session = session

        # Local staging controls consume these values without allowing a
        # static command line to accidentally bind the wrong release session.
        os.environ["COVERAGE_VALIDATION_SESSION_MANIFEST"] = \
            self.validation_session_manifest_path
        os.environ["COVERAGE_VALIDATION_SESSION_ID"] = str(
            session.data.get("session_id")
        )
        os.environ["COVERAGE_VALIDATION_CANDIDATE_SHA"] = str(
            identity.get("commit_sha")
        )
        os.environ["COVERAGE_VALIDATION_BASELINE_SHA"] = str(
            previous_release.get("commit_sha")
        )
        os.environ["COVERAGE_VALIDATION_TEARDOWN_EVIDENCE"] = \
            self.validation_teardown_evidence_path
        self._configure_serving_session(upgrade_config, previous_release)

    def _ensure_flat_current_baseline(self, upgrade_config, previous_release):
        """Perform the explicit Flat adoption at the cutover boundary only."""
        if self._deployment_layout != FLAT or not self._current_adoption_plan:
            return None
        if not upgrade_config.get("flat_current_adoption_on_cutover"):
            raise RuntimeError(
                "Flat deployment requires explicit flat_current_adoption_on_cutover"
            )
        identity_path = upgrade_config.get("flat_release_identity_path") or \
            upgrade_config.get("legacy_flat_release_identity_path", "")
        flat_root = upgrade_config.get("flat_served_root") or upgrade_config.get(
            "legacy_flat_served_root", ""
        )
        if identity_path and not os.path.isabs(str(identity_path)):
            identity_path = os.path.join(self.repo_root, str(identity_path))
        if flat_root and not os.path.isabs(str(flat_root)):
            flat_root = os.path.join(self.repo_root, str(flat_root))
        baseline_session = str(
            upgrade_config.get("flat_baseline_session_id") or ""
        ).strip()
        if not baseline_session:
            raise RuntimeError(
                "flat_baseline_session_id is required for explicit adoption"
            )
        application_root = ""
        if self._production_lifecycle_adapter is not None:
            application_root = str(
                self._production_lifecycle_adapter.config.get(
                    "legacy_application_root"
                ) or ""
            ).strip()
            if not application_root:
                raise RuntimeError(
                    "vfoswind Flat adoption requires legacy_application_root"
                )
        result = bootstrap_flat_current(
            self.publish_root, flat_root, identity_path,
            previous_release.get("commit_sha", ""), baseline_session,
            switch=True,
            api_contract_version=upgrade_config.get("api_contract_version", ""),
            application_root=application_root,
        )
        if result.get("status") != "PASSED":
            raise RuntimeError("Flat baseline adoption did not pass")
        current = self.publisher.validate_current()
        if current.get("status") != "PASSED":
            raise RuntimeError("adopted baseline CURRENT failed validation")
        current_binding = current_served_root_binding(self.publish_root)
        if current_binding.get("previous_release_commit_sha") != \
                previous_release.get("commit_sha"):
            raise RuntimeError("adopted baseline CURRENT commit does not match rollback identity")
        candidate_binding = verify_production_candidate_served_root_binding(
            self.candidate_preflight.get("source_provenance") or {},
            current_binding,
        )
        self.candidate_preflight["candidate_served_root_binding"] = candidate_binding
        self.candidate_preflight["current_served_root_binding"] = current_binding
        self.previous_published_session_id = self.publisher.current_session_id()
        self._deployment_layout = IMMUTABLE_CURRENT
        result["deployment_layout_after_adoption"] = IMMUTABLE_CURRENT
        self.manifest.record("flat_current_adoption", result)
        return result

    @staticmethod
    def _selected_database_identity(connection):
        """Read the selected database without mutating either connection."""
        with connection.cursor() as cursor:
            cursor.execute("SELECT DATABASE() AS database_name")
            row = cursor.fetchone() or {}
        if isinstance(row, dict):
            return str(row.get("database_name") or row.get("DATABASE()") or "")
        return str(row[0] if row else "")

    def _reconfirm_cutover_identity(self, connection, target_connection,
                                    db_config, target_db_config,
                                    previous_release):
        """Recheck all mutable identities immediately before stopping CURRENT."""
        if connection is None or target_connection is None:
            raise RuntimeError("source and target connections are required for cutover identity")
        source_generation = inspect_database_generation(connection)
        if source_generation.get("generation") != \
                self._database_generation.get("generation"):
            raise RuntimeError("source database generation changed before cutover")
        target_generation = inspect_database_generation(target_connection)
        if target_generation.get("generation") != \
                self._target_database_generation.get("generation"):
            raise RuntimeError("target database generation changed before cutover")

        source_cfg = dict(db_config or {})
        if isinstance(source_cfg.get("mysql"), dict):
            source_cfg = dict(source_cfg["mysql"])
        target_cfg = dict(target_db_config or {})
        if isinstance(target_cfg.get("mysql"), dict):
            target_cfg = dict(target_cfg["mysql"])
        selected_source = self._selected_database_identity(connection)
        selected_target = self._selected_database_identity(target_connection)
        expected_source = str(source_cfg.get("database") or "")
        expected_target = str(target_cfg.get("database") or "")
        if expected_source and selected_source.lower() != expected_source.lower():
            raise RuntimeError(
                "source database identity changed before cutover: {}".format(
                    selected_source
                )
            )
        if expected_target and selected_target.lower() != expected_target.lower():
            raise RuntimeError(
                "target database identity changed before cutover: {}".format(
                    selected_target
                )
            )
        if selected_source and selected_target and \
                selected_source.lower() == selected_target.lower():
            raise RuntimeError("source and target selected database identities are equal")

        current_path = os.path.join(self.publish_root, "CURRENT")
        current = {}
        if self._deployment_layout == IMMUTABLE_CURRENT:
            current = current_served_root_binding(self.publish_root)
            if str(current.get("previous_release_commit_sha") or "").lower() != \
                    str(previous_release.get("commit_sha") or "").lower():
                raise RuntimeError("CURRENT baseline identity changed before cutover")
            if self.previous_published_session_id and \
                    current.get("release_validation_session_id") != \
                    self.previous_published_session_id:
                raise RuntimeError("CURRENT validation session changed before cutover")
        elif self._deployment_layout == FLAT:
            if os.path.lexists(current_path):
                raise RuntimeError(
                    "Flat deployment changed to CURRENT before the cutover boundary"
                )
        else:
            raise RuntimeError("deployment layout changed before cutover")
        return {
            "status": "PASSED",
            "source_generation": source_generation,
            "target_generation": target_generation,
            "source_database": selected_source,
            "target_database": selected_target,
            "deployment_layout": self._deployment_layout,
            "current": current,
            "current_path": current_path,
            "command": "inspect_database_generation + SELECT DATABASE() + CURRENT binding",
            "exit_code": 0,
        }

    def _configure_serving_session(self, upgrade_config: Dict[str, Any],
                                   previous_release: Dict[str, Any]):
        """Bind a stable owner for the process behind CURRENT.

        Validation sessions are per-attempt.  The final serving owner is not:
        its manifest, PID file, and state pointer survive a successful
        upgrade so the next attempt can stop the actual CURRENT process.
        """
        serving_id = str(upgrade_config.get("serving_session_id") or "").strip()
        if not serving_id:
            raise RuntimeError("upgrade.serving_session_id is required")
        serving_manifest = _resolve_upgrade_path(
            self.repo_root, upgrade_config.get("serving_session_manifest"),
            "serving_session_manifest",
        )
        serving_teardown = _resolve_upgrade_path(
            self.repo_root,
            upgrade_config.get("serving_teardown_evidence_path") or
            (serving_manifest + ".teardown.json"),
            "serving_teardown_evidence_path",
        )
        state_path = _resolve_upgrade_path(
            self.repo_root, upgrade_config.get("current_serving_state_path"),
            "current_serving_state_path",
        )
        if len(serving_id) > 128:
            raise RuntimeError("upgrade.serving_session_id is too long")
        if serving_manifest in (self.validation_session_manifest_path,
                                self.validation_teardown_evidence_path):
            raise RuntimeError("serving session manifest must be separate from validation evidence")
        if serving_teardown in (serving_manifest, self.validation_session_manifest_path,
                                self.validation_teardown_evidence_path):
            raise RuntimeError("serving teardown evidence must be a separate artifact")
        if state_path in (serving_manifest, serving_teardown,
                          self.validation_session_manifest_path,
                          self.validation_teardown_evidence_path):
            raise RuntimeError("current serving state must be a separate artifact")

        if os.path.isfile(serving_manifest):
            serving_session = ValidationSession.load(serving_manifest)
            if serving_session.data.get("session_id") != serving_id:
                raise RuntimeError("serving session id does not match stable manifest")

        self.serving_session_id = serving_id
        self.serving_session_manifest_path = serving_manifest
        self.serving_teardown_evidence_path = serving_teardown
        self.serving_state_path = state_path
        os.environ["COVERAGE_SERVING_SESSION_MANIFEST"] = serving_manifest
        os.environ["COVERAGE_SERVING_SESSION_ID"] = serving_id
        os.environ["COVERAGE_SERVING_CANDIDATE_SHA"] = str(
            previous_release.get("commit_sha") or ""
        )
        os.environ["COVERAGE_SERVING_BASELINE_SHA"] = str(
            previous_release.get("commit_sha") or ""
        )
        os.environ["COVERAGE_SERVING_TEARDOWN_EVIDENCE"] = serving_teardown
        os.environ["COVERAGE_SERVING_STATE_PATH"] = state_path
        os.environ["COVERAGE_SERVING_RELEASE_SESSION_ID"] = str(
            self.previous_published_session_id or ""
        )

    def _teardown_validation_session(self, identity: Dict[str, Any], mode: str,
                                      lifecycle=None):
        if self.validation_session is None:
            return None
        if self._validation_teardown_result is not None:
            return self._validation_teardown_result

        service_stop = {}
        service_stop_error = ""
        # A systemd-managed validation service is not a child of the
        # controller and therefore is not stopped by ValidationSession's PID
        # signals alone. Stop the explicit validation owner first, then use
        # the session manifest/port probe as the independent closure check.
        if lifecycle is not None and lifecycle.api_started:
            try:
                service_stop = lifecycle.stop_validation_api()
            except Exception as exc:
                service_stop_error = str(exc)
                self.log(
                    "❌ Validation API stop failed: {}".format(service_stop_error)
                )

        try:
            result = self.validation_session.teardown(
                evidence_path=self.validation_teardown_evidence_path,
                timeout=float(
                    ((self._runtime_config or {}).get("upgrade") or {}).get(
                        "validation_teardown_timeout_sec", 10
                    )
                ),
            )
        except Exception as exc:
            result = {
                "schema_version": SESSION_SCHEMA_VERSION,
                "session_id": self.validation_session.data.get("session_id", ""),
                "status": "FAILED",
                "pids_closed": False,
                "ports_closed": False,
                "ports_probe_ok": False,
                "attempted": [],
                "verification": {},
                "violations": ["ValidationSession teardown failed: {}".format(exc)],
                "p1": True,
            }
            self.log("❌ Validation session teardown failed: {}".format(exc))

        if service_stop_error:
            result["status"] = "FAILED"
            result.setdefault("violations", []).append(
                "validation API stop failed: {}".format(service_stop_error)
            )
            result["pids_closed"] = False
            result["ports_closed"] = False
            result["ports_probe_ok"] = False
        result["validation_api_stop"] = service_stop
        if result.get("status") == "PASSED" and \
                self._production_lifecycle_adapter is not None and \
                self._production_lifecycle_adapter.validation_binding_changed:
            try:
                restored_validation = \
                    self._production_lifecycle_adapter.restore_validation_candidate_binding()
                daemon_reload = self._production_lifecycle_adapter.daemon_reload()
                result["production_validation_binding_restore"] = {
                    "status": "PASSED",
                    "binding": restored_validation,
                    "daemon_reload": daemon_reload,
                    "credentials_written_to_evidence": False,
                    "exit_code": 0,
                }
            except Exception as exc:
                result["status"] = "FAILED"
                result.setdefault("violations", []).append(
                    "validation systemd binding restore failed: {}".format(exc)
                )
                self.log(
                    "❌ Validation systemd binding restore failed: {}".format(exc)
                )

        verification = result.get("verification") or {}
        result["pids_closed"] = verification.get("pids_closed") is True
        result["ports_closed"] = verification.get("ports_closed") is True
        result["ports_probe_ok"] = verification.get("ports_probe_ok") is True
        if service_stop_error:
            result["pids_closed"] = False
            result["ports_closed"] = False
            result["ports_probe_ok"] = False
        result.update({
            "revision": identity.get("commit_sha"),
            "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
            "command": "ValidationSession.teardown",
            "exit_code": 0 if result.get("status") == "PASSED" else 1,
            "artifact_path": self.validation_teardown_evidence_path,
        })
        self.manifest.record("validation_teardown", result)

        session_record = dict(self.validation_session.data)
        session_record.update({
            "status": "PASSED" if (
                session_record.get("session_id") and
                session_record.get("candidate_sha") == identity.get("commit_sha") and
                session_record.get("baseline_sha")
            ) else "FAILED",
            "revision": identity.get("commit_sha"),
            "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
            "command": "ValidationSession.load",
            "exit_code": 0,
            "artifact_path": self.validation_session_manifest_path,
        })
        self.manifest.record("validation_session_manifest", session_record)
        self._validation_teardown_result = result
        return result

    def _activate_final_serving_session(self):
        """Bind the final serving process to a session separate from validation."""
        if self.validation_session is None:
            raise RuntimeError("validation session is required before final serving")
        validation_id = str(self.validation_session.data.get("session_id") or "")
        if not validation_id or not self.serving_session_id:
            raise RuntimeError("validation session identity is missing")
        # local_staging_control consumes the serving namespace in preference
        # to the validation namespace.  Production supervisors may ignore
        # these values, but the lifecycle command remains explicitly distinct.
        os.environ["COVERAGE_SERVING_SESSION_MANIFEST"] = self.serving_session_manifest_path
        os.environ["COVERAGE_SERVING_SESSION_ID"] = self.serving_session_id
        os.environ["COVERAGE_SERVING_CANDIDATE_SHA"] = str(
            self.validation_session.data.get("candidate_sha") or ""
        )
        os.environ["COVERAGE_SERVING_BASELINE_SHA"] = str(
            self.validation_session.data.get("baseline_sha") or ""
        )
        os.environ["COVERAGE_SERVING_TEARDOWN_EVIDENCE"] = self.serving_teardown_evidence_path
        os.environ["COVERAGE_SERVING_STATE_PATH"] = self.serving_state_path
        os.environ["COVERAGE_SERVING_RELEASE_SESSION_ID"] = self.release_validation_session_id

    def log(self, msg: str):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}")
        self.logs.append(msg)
        self.manifest.add_log(msg)

    def _mark_not_ready(self):
        """Make every failed/unfinished execution explicitly NOT_READY."""
        self.manifest.data["release_decision"] = "NOT_READY"
        if self.manifest.data.get("status") == "UPGRADE_SUCCESS":
            self.manifest.data["status"] = "UNMET_GATES"
        self.manifest.save()

    def _validate_pre_cutover_ready(self, identity, mode):
        """Record a hard PRE_CUTOVER_READY decision before Phase D.

        This method intentionally has no lifecycle, publication, database,
        systemd, Nginx, or traffic side effects.  A failed result is recorded
        as ``PRE_CUTOVER_READY=FAILED`` and the caller must return through
        ``_fail`` without entering the cutover block.
        """
        try:
            passed, unmet = self.manifest.validate_pre_cutover_gate(
                require_production_integration=mode == "production"
            )
        except Exception as exc:
            passed = False
            unmet = ["pre-cutover gate evaluation failed: {}".format(exc)]
        payload = {
            "status": "PASSED" if passed else "FAILED",
            "phase": "PRE_CUTOVER_READY",
            "revision": identity.get("commit_sha"),
            "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
            "current_unchanged_until_phase_d": True,
            "phase_d_entered": False,
            "freeze_called": False,
            "stop_production_called": False,
            "current_switch_called": False,
            "unmet": list(unmet),
            "command": "ProductionEvidenceManifest.validate_pre_cutover_gate",
            "exit_code": 0 if passed else 1,
        }
        self.manifest.record("pre_cutover_ready", payload)
        return passed, unmet

    def _cleanup_target_database(self):
        """Remove only the disposable target after a failed attempt."""
        if self._target_cleanup_done or not self._target_preparation:
            return {"status": "PASSED", "skipped": True, "reason": "no_disposable_target"}
        if not self._target_preparation.get("target_database_created_by_this_run"):
            return {"status": "PASSED", "skipped": True, "reason": "target_not_created_by_run"}
        from scripts.upgrade.disposable_target import cleanup_disposable_target
        result = cleanup_disposable_target(
            self._target_db_config, self._target_preparation
        )
        self._target_cleanup_done = True
        return result

    def _fail(self, lifecycle: Optional[UpgradeLifecycle], message: str) -> Tuple[bool, str]:
        self._mark_not_ready()
        # If a vfoswind serving process has already been started with the new
        # persistent DB binding, stop it before restoring CURRENT and the old
        # EnvironmentFile.  This keeps rollback from restarting a process with
        # a mixed release/database identity.
        if self._production_runtime_bound and lifecycle is not None and \
                lifecycle.serving_api_started:
            try:
                stop_serving = lifecycle.stop_serving_api()
                self.manifest.record("production_integration_rollback", {
                    "status": "PASSED",
                    "phase": "stop_candidate_serving",
                    "command": stop_serving,
                    "credentials_written_to_evidence": False,
                    "exit_code": 0,
                })
            except Exception as exc:
                self.log(
                    "❌ DATA_SAFETY_HOLD: candidate serving stop failed: {}".format(
                        exc
                    )
                )
        if self._publication_switched:
            try:
                if self.publisher is None or not self.previous_published_session_id:
                    raise RuntimeError("immutable rollback identity is unavailable")
                rollback = self.publisher.rollback(self.previous_published_session_id)
                if rollback.get("status") != "PASSED":
                    raise RuntimeError("immutable rollback did not pass")
                self._publication_switched = False
                self.log("✔ Immutable CURRENT pointer rolled back before traffic was opened.")
            except Exception as exc:
                self.log("❌ DATA_SAFETY_HOLD: immutable publication rollback failed: {}".format(exc))
        if self._production_release_bindings_changed and \
                self._production_lifecycle_adapter is not None:
            try:
                restored_bindings = self._production_lifecycle_adapter.restore_previous_release_bindings()
                daemon_reload = self._production_lifecycle_adapter.daemon_reload()
                self.manifest.record("production_integration_rollback", {
                    "status": "PASSED",
                    "phase": "restore_previous_release_bindings",
                    "release_bindings": restored_bindings,
                    "daemon_reload": daemon_reload,
                    "exit_code": 0,
                })
                self._production_release_bindings_changed = False
            except Exception as exc:
                self.log(
                    "❌ DATA_SAFETY_HOLD: production release binding rollback failed: {}".format(
                        exc
                    )
                )
        if self._production_runtime_bound and self._production_lifecycle_adapter is not None:
            try:
                restored = self._production_lifecycle_adapter.restore_previous_database_binding()
                reload_evidence = self._production_lifecycle_adapter.daemon_reload()
                self.manifest.record("production_integration_rollback", {
                    "status": "PASSED",
                    "phase": "restore_previous_database_binding",
                    "runtime_binding": restored,
                    "daemon_reload": reload_evidence,
                    "credentials_written_to_evidence": False,
                    "exit_code": 0,
                })
                self._production_runtime_bound = False
            except Exception as exc:
                self.log(
                    "❌ DATA_SAFETY_HOLD: production runtime binding rollback failed: {}".format(
                        exc
                    )
                )
        if lifecycle is not None and lifecycle.active:
            try:
                rollback_result = lifecycle.abort()
                self.log("✔ Upgrade lifecycle aborted and previous API restored: {}".format(
                    rollback_result.get("previous_release_verified", False)))
                if self._production_lifecycle_adapter is not None:
                    nginx_rollback = self._production_lifecycle_adapter.reload_nginx()
                    self.manifest.record("production_integration_rollback", {
                        "status": "PASSED",
                        "phase": "nginx_reload_previous_current",
                        "nginx": nginx_rollback,
                        "credentials_written_to_evidence": False,
                        "exit_code": 0,
                    })
            except Exception as exc:
                self.log("❌ DATA_SAFETY_HOLD: lifecycle abort failed: {}".format(exc))
        if self.validation_session is not None:
            try:
                teardown = self._teardown_validation_session(
                    self._target_identity, self._upgrade_mode, lifecycle=lifecycle
                )
                if teardown and teardown.get("status") != "PASSED":
                    self.log("❌ Release NOT_READY: validation session teardown did not pass.")
            except Exception as exc:
                self.log("❌ DATA_SAFETY_HOLD: validation session teardown evidence failed: {}".format(exc))
        try:
            target_cleanup = self._cleanup_target_database()
            if target_cleanup.get("status") != "PASSED":
                self.log(
                    "❌ DATA_SAFETY_HOLD: disposable target cleanup failed: {}".format(
                        target_cleanup
                    )
                )
            else:
                self.manifest.record("disposable_target_cleanup", {
                    "status": "PASSED",
                    "revision": self._target_identity.get("commit_sha"),
                    "evidence_class": "blue_green_database",
                    "cleanup": target_cleanup,
                    "command": "revoke Candidate DB grant + DROP disposable target",
                    "exit_code": 0,
                })
        except Exception as exc:
            self.log(
                "❌ DATA_SAFETY_HOLD: disposable target cleanup failed: {}".format(
                    exc
                )
            )
        return False, message

    def _verify_release_endpoint(self, endpoint: str, identity: Dict[str, Any]) -> Dict[str, Any]:
        if not endpoint:
            raise RuntimeError("upgrade.release_endpoint is required")
        with urllib.request.urlopen(endpoint, timeout=10) as response:
            if int(getattr(response, "status", 200)) != 200:
                raise RuntimeError("release endpoint returned HTTP {}".format(response.status))
            payload = json.loads(response.read().decode("utf-8"))
        actual = payload.get("release") if isinstance(payload, dict) else None
        if not isinstance(actual, dict):
            raise RuntimeError("release endpoint did not return a release identity")
        for key in (
                "version", "commit_sha", "build_id", "asset_hash", "schema_version",
                "asset_manifest_version", "asset_count", "asset_manifest_hash",
                "asset_manifest"):
            if actual.get(key) != identity.get(key):
                raise RuntimeError("release endpoint mismatch: {}".format(key))
        return {
            "status": "PASSED", "evidence_class": "staging_cutover",
            "endpoint": endpoint, "release": actual,
            "command": "GET {}".format(endpoint), "exit_code": 0,
        }

    def _verify_health_endpoint(self, endpoint: str) -> Dict[str, Any]:
        if not endpoint:
            raise RuntimeError("upgrade.health_endpoint is required")
        with urllib.request.urlopen(endpoint, timeout=10) as response:
            status = int(getattr(response, "status", 200))
            payload = json.loads(response.read().decode("utf-8"))
        if status != 200:
            raise RuntimeError("health endpoint returned HTTP {}".format(status))
        if not isinstance(payload, dict) or payload.get("status") not in (
                "ok", "healthy", "PASSED"):
            raise RuntimeError("health endpoint did not report a healthy runtime")
        return {
            "status": "PASSED", "evidence_class": "staging_cutover",
            "endpoint": endpoint, "health": payload,
            "command": "GET {}".format(endpoint), "exit_code": 0,
        }

    def _verify_post_open_serving(self, identity: Dict[str, Any]) -> Dict[str, Any]:
        """Verify the process serving traffic after the traffic switch."""
        upgrade_config = ((self._runtime_config or {}).get("upgrade") or {})
        release_endpoint = upgrade_config.get("release_endpoint")
        health_endpoint = upgrade_config.get("health_endpoint")
        release = self._verify_release_endpoint(release_endpoint, identity)
        health = self._verify_health_endpoint(health_endpoint)
        if self.publisher is None or self.validation_session is None:
            raise RuntimeError("immutable publisher and validation session are required")
        current = self.publisher.validate_current()
        if current.get("status") != "PASSED":
            raise RuntimeError(
                "CURRENT failed post-open validation: {}".format(
                    "; ".join(current.get("violations") or [])
                )
            )
        expected_session = self.validation_session.data.get("session_id")
        if current.get("commit_sha") != identity.get("commit_sha"):
            raise RuntimeError("post-open CURRENT commit does not match target release")
        if current.get("release_validation_session_id") != expected_session:
            raise RuntimeError("post-open CURRENT session does not match target release")
        served_root_probe = verify_http_served_root(
            upgrade_config.get("served_root_probe_url"),
            self.publisher.release_path(expected_session),
            configured_served_root_path=self.served_root_path,
            url_prefix=upgrade_config.get("served_root_url_prefix", "/coverage/"),
            relative_path=upgrade_config.get("served_root_probe_relative_path", ""),
        )
        return {
            "status": "PASSED",
            "revision": identity.get("commit_sha"),
            "evidence_class": "staging_cutover" if self._upgrade_mode == "staging" else "production_cutover",
            "process_role": "production_serving",
            "release_endpoint": release,
            "health_endpoint": health,
            "served_root": current.get("served_root"),
            "served_root_sha256": self._served_root_sha256,
            "release_validation_session_id": expected_session,
            "candidate_artifact_sha256": self._candidate_artifact_sha256,
            "current_release_validation_session_id": current.get("release_validation_session_id"),
            "expected_release_validation_session_id": expected_session,
            "publisher_current_validation": current,
            "served_root_http": served_root_probe,
            "command": "GET release + GET health + HTTP Served Root byte identity + ImmutableReleasePublisher.validate_current",
            "exit_code": 0,
        }

    def execute_upgrade(self, dry_run: bool = False, connection=None, db_config=None,
                        mode: str = "staging", deployment_manifest: Optional[str] = None,
                        target_release: Optional[Dict[str, Any]] = None,
                        runtime_config: Optional[Dict[str, Any]] = None,
                        target_connection=None, target_db_config=None,
                        target_connection_factory=None) -> Tuple[bool, str]:
        """Run complete upgrade procedure."""
        self._runtime_config = dict(runtime_config or {})
        self._upgrade_mode = mode
        self._production_lifecycle_adapter = None
        self._production_runtime_bound = False
        self._production_release_bindings_changed = False
        self._target_preparation = {}
        self._target_cleanup_done = False
        self._mark_not_ready()
        self.manifest.data["upgrade_mode"] = mode
        self.manifest.save()
        self.log("=== Starting Manifest-Driven Upgrade Procedure ===")
        if mode not in ("staging", "production"):
            return False, "Invalid upgrade mode"
        state = (runtime_config or {}).get("runtime_state") or {}
        registry_root = state.get("root") or os.path.join(self.repo_root, ".runtime-state")
        if not os.path.isabs(registry_root):
            registry_root = os.path.join(self.repo_root, registry_root)
        registry_dir = state.get("registry_dir", "report-registry")
        os.environ["COVERAGE_REGISTRY_DIR"] = os.path.realpath(os.path.join(registry_root, registry_dir))
        if not deployment_manifest or not os.path.isfile(deployment_manifest):
            return False, "Explicit deployment manifest is required"
        try:
            with open(deployment_manifest, "r", encoding="utf-8") as stream:
                deployment = json.load(stream)
            actions = deployment.get("actions")
            if not isinstance(actions, list) or not actions:
                return False, "Deployment manifest has no explicit actions"
            for action in actions:
                if action.get("op") not in ("ADD", "MODIFY", "MOVE", "DELETE"):
                    return False, "Deployment manifest contains invalid operation"
                if any(ch in str(action.get("source", "")) for ch in ("*", "?")):
                    return False, "Wildcard deployment action is forbidden"
                if action.get("op") not in ("ADD", "MODIFY"):
                    return False, "Only explicit ADD/MODIFY cutover actions are supported"
        except (OSError, ValueError, TypeError) as exc:
            return False, "Invalid deployment manifest: {}".format(exc)
        
        # Step 1: Precheck & Identity Verification
        self.log("[Step 1/10] Verifying Release Identity & Git Tree...")
        try:
            identity = verify_release_identity(self.repo_root, target_release)
        except RuntimeError as exc:
            self.log("❌ Release identity preflight failed: {}".format(exc))
            return False, "Release identity mismatch"
        identity_evidence = dict(identity)
        identity_evidence.update({
            "name": "release_identity",
            "command": "verify_release_identity",
            "exit_code": 0,
        })
        self.manifest.record("release_identity", identity_evidence)
        self._target_identity = dict(identity)
        self.log(f"  Target Release: {identity.get('version')} (Build: {identity.get('build_id')})")

        upgrade_config = (runtime_config or {}).get("upgrade") or {}
        self._target_db_config = dict(
            target_db_config or upgrade_config.get("target_mysql") or
            (runtime_config or {}).get("target_mysql") or {}
        )
        if not dry_run and connection is None:
            return False, "Live source database connection required"
        target_connection_deferred = (
            not dry_run and target_connection is None and
            callable(target_connection_factory)
        )
        configured_target_mode = str(
            upgrade_config.get("target_preparation_mode") or ""
        ).strip()
        if configured_target_mode and configured_target_mode not in \
                DISPOSABLE_TARGET_MODES:
            return False, "Unknown disposable target preparation mode"
        if not dry_run and target_connection is not None and \
                configured_target_mode != PRE_RESTORED_CONSISTENT_BACKUP:
            return False, (
                "pre-restored target connections require explicit "
                "target_preparation_mode=pre_restored_consistent_backup"
            )
        if not dry_run and target_connection is None and \
                not target_connection_deferred:
            return False, "Disposable target factory or pre-restored target is required"
        if not dry_run:
            try:
                validate_disposable_target_config(db_config or {}, self._target_db_config)
            except (RuntimeError, ValueError, TypeError) as exc:
                self.log("❌ Disposable target application access preflight failed: {}".format(exc))
                return False, "Disposable target application access preflight failed"
        if connection is not None:
            self._database_generation = inspect_database_generation(connection)
            if self._database_generation.get("generation") == UNKNOWN:
                self.manifest.record("database_generation", {
                    "status": "FAILED", "generation": UNKNOWN,
                    "evidence_class": "production_database",
                    "revision": identity.get("commit_sha"),
                    "reason": self._database_generation.get("reason"),
                    "tables": self._database_generation.get("tables", []),
                    "command": "inspect_database_generation(source)",
                    "exit_code": 1,
                })
                return False, "Database generation is UNKNOWN; upgrade blocked"
            self.manifest.record("database_generation", {
                "status": "PASSED", "generation": self._database_generation.get("generation"),
                "evidence_class": "production_database",
                "revision": identity.get("commit_sha"),
                "reason": self._database_generation.get("reason"),
                "tables": self._database_generation.get("tables", []),
                "command": "inspect_database_generation(source)",
                "exit_code": 0,
            })
            if not dry_run and target_connection is None and \
                    not target_connection_deferred:
                return False, "Disposable target database connection is required"
            if not dry_run and target_connection is None and \
                    not (self._target_db_config or {}).get("database"):
                return False, "Disposable target database configuration is required"
            if target_connection is not None:
                try:
                    candidate_access = probe_candidate_connection_access(
                        target_connection, self._target_db_config
                    )
                    separation = validate_migration_database_separation(
                        db_config or {}, self._target_db_config,
                        source_connection=connection,
                        target_connection=target_connection,
                    )
                except (RuntimeError, ValueError, TypeError) as exc:
                    self.manifest.record("database_separation", {
                        "status": "FAILED", "revision": identity.get("commit_sha"),
                        "evidence_class": "production_database",
                        "command": "validate_migration_database_separation",
                        "exit_code": 1, "violations": [str(exc)],
                    })
                    return False, "Source/target database separation failed"
                target_generation = inspect_database_generation(target_connection)
                self._target_database_generation = target_generation
                source_generation = self._database_generation.get("generation")
                target_generation_value = target_generation.get("generation")
                if source_generation == VNEXT and target_generation_value != VNEXT:
                    return False, "Existing-VNext target must be a consistent VNext backup"
                if source_generation == LEGACY and target_generation_value == LEGACY:
                    return False, "Legacy migration target must be empty/new VNext"
                self.manifest.record("database_separation", {
                    "status": "PASSED", "revision": identity.get("commit_sha"),
                    "evidence_class": "production_database",
                    "separation": separation,
                    "target_generation": target_generation,
                    "candidate_access": candidate_access,
                    "command": "validate_migration_database_separation + inspect_database_generation(target)",
                    "exit_code": 0,
                })

        # Step 2: Schema Preflight
        self.log("[Step 2/10] Running Static DDL Preflight Validation...")
        ddl_name = "vnext_schema_v3.sql" if self._database_generation.get(
            "generation"
        ) == VNEXT else "vnext_schema.sql"
        ddl_path = os.path.join(self.repo_root, "scripts", "upgrade", ddl_name)
        safe, errs, warns = validate_ddl_file(ddl_path)
        if not safe:
            self.log(f"❌ Schema preflight failed: {errs}")
            return False, "Schema preflight rejected DDL script"
        self.manifest.record("schema_migration", {
            "preflight_safe": True,
            "status": "PASSED",
            "revision": identity.get("commit_sha"),
            "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
            "ddl_script": ddl_name,
            "warnings": warns,
            "command": "validate_ddl_file scripts/upgrade/{}".format(ddl_name),
            "exit_code": 0,
        })
        self.log("✔ Schema preflight check passed (Additive & Idempotent).")

        self._configure_backup_root(upgrade_config.get("backup_root"))
        previous_release = upgrade_config.get("previous_release")
        if not isinstance(previous_release, dict) or not previous_release:
            # A target release must never be used as its own rollback proof.
            # Staging and production both need an independently captured
            # before-release identity from the previous deployment.
            self.log("❌ Explicit previous_release identity is required for rollback evidence")
            return False, "Explicit previous release identity required"
        if previous_release.get("commit_sha") == identity.get("commit_sha"):
            self.log("❌ previous_release must differ from target release")
            return False, "Previous and target release identities are identical"
        try:
            self._configure_release_controls(
                upgrade_config, identity, previous_release, self._runtime_config
            )
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            self.log("❌ Immutable publication/session preflight failed: {}".format(exc))
            return False, "Immutable publication/session preflight failed"
        if mode == "production":
            production_integration = upgrade_config.get(
                "production_integration"
            ) or {}
            if str(upgrade_config.get("lifecycle_adapter") or "").strip() != \
                    VFOSWIND_ADAPTER or \
                    str(production_integration.get("adapter") or "").strip() != \
                    VFOSWIND_ADAPTER:
                self.log(
                    "❌ Production lifecycle adapter must explicitly be {}".format(
                        VFOSWIND_ADAPTER
                    )
                )
                return False, "Production integration adapter is not configured"
            try:
                self._production_lifecycle_adapter = VfoswindProductionLifecycle(
                    self.publish_root, production_integration
                )
                source_runtime_config = dict(db_config or {})
                if isinstance(source_runtime_config.get("mysql"), dict):
                    source_runtime_config = dict(source_runtime_config["mysql"])
                integration_evidence = self._production_lifecycle_adapter.preflight(
                    expected_database=source_runtime_config.get("database", ""),
                    candidate_application_root=os.path.join(
                        self.production_candidate_root, "app"
                    ),
                    candidate_ports=upgrade_config.get("validation_ports") or [],
                    validation_commands=upgrade_config.get("commands") or {},
                    runtime_mysql=source_runtime_config,
                )
                integration_evidence.update({
                    "revision": identity.get("commit_sha"),
                    "evidence_class": "production_integration",
                })
                self.manifest.record(
                    "production_integration_preflight", integration_evidence
                )
                bootstrap_evidence = dict(
                    integration_evidence.get("bootstrap") or {}
                )
                if not bootstrap_evidence and \
                        integration_evidence.get("bootstrap_ready") is not None:
                    # The vfoswind adapter returns the Flat transition plan as
                    # its top-level preflight result.  Preserve that evidence
                    # in the dedicated bootstrap record instead of silently
                    # downgrading a required bootstrap to "already present".
                    bootstrap_evidence = {
                        key: integration_evidence[key]
                        for key in (
                            "status", "bootstrap_ready", "bootstrap_required",
                            "deployment_layout", "transition_required",
                            "legacy_systemd_unit", "legacy_systemd_unit_file",
                            "legacy_nginx_config_path", "legacy_application_root",
                            "legacy_served_root", "managed_systemd_unit_file",
                            "managed_nginx_config_path", "runtime_database",
                            "runtime_application_user", "candidate_application_root",
                            "candidate_artifact_sha256", "validation_ports",
                            "old_unit_sha256", "old_nginx_sha256",
                            "managed_unit_sha256", "managed_nginx_sha256",
                            "rollback_bytes_verified", "systemd_analyze", "nginx_test",
                            "read_only", "command", "exit_code",
                        ) if key in integration_evidence
                    }
                if not bootstrap_evidence:
                    bootstrap_evidence = {
                        "status": "PASSED",
                        "bootstrap_ready": True,
                        "bootstrap_required": False,
                        "deployment_layout": integration_evidence.get(
                            "deployment_layout", ""
                        ),
                        "read_only": True,
                        "command": "managed vfoswind layout already present",
                        "exit_code": 0,
                    }
                bootstrap_evidence.update({
                    "revision": identity.get("commit_sha"),
                    "evidence_class": "production_integration",
                })
                self.manifest.record("production_bootstrap", bootstrap_evidence)
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                self.log(
                    "❌ vfoswind systemd/Nginx integration preflight failed: {}".format(
                        exc
                    )
                )
                return False, "Production systemd/Nginx integration is not closed"
        self.manifest.record("deployment_layout", {
            "status": "PASSED",
            "deployment_layout": self._deployment_layout,
            "revision": identity.get("commit_sha"),
            "evidence_class": "production_deployment",
            "flat_current_adoption": self._current_adoption_plan or {},
            "command": "validate_current_or_plan_flat",
            "exit_code": 0,
        })
        configured_residue_roots = upgrade_config.get("validation_residue_roots") or []
        if isinstance(configured_residue_roots, (str, int)):
            configured_residue_roots = [configured_residue_roots]
        residue_roots = list(configured_residue_roots)
        if upgrade_config.get("validation_candidate_root"):
            residue_roots.append(upgrade_config.get("validation_candidate_root"))
        residue_ports = upgrade_config.get("validation_residue_ports")
        if residue_ports is None:
            residue_ports = upgrade_config.get("validation_ports") or []
        configured_residue_manifests = upgrade_config.get(
            "validation_residue_session_manifests"
        ) or []
        if isinstance(configured_residue_manifests, str):
            configured_residue_manifests = [configured_residue_manifests]
        residue_manifests = []
        for manifest_path in configured_residue_manifests:
            manifest_path = str(manifest_path)
            if not os.path.isabs(manifest_path):
                manifest_path = os.path.join(self.repo_root, manifest_path)
            residue_manifests.append(manifest_path)
        residue_session = str(
            upgrade_config.get("validation_residue_session_id") or
            self.release_validation_session_id
        ).strip()
        residue = scan_validation_residue(
            candidate_roots=residue_roots, ports=residue_ports,
            session_identity=residue_session,
            session_manifests=residue_manifests,
        )
        residue.update({
            "revision": identity.get("commit_sha"),
            "evidence_class": "validation_process_ownership",
            "command": "scan_validation_residue",
            "exit_code": 0 if residue.get("status") != RESIDUE_BLOCKED else 1,
        })
        self.manifest.record("validation_residue_gate", residue)
        if residue.get("status") == RESIDUE_BLOCKED:
            self.log("❌ Validation residue gate blocked the upgrade.")
            return False, "Validation residue gate blocked"
        if residue.get("status") == SAFE_TO_TEARDOWN and \
                upgrade_config.get("validation_residue_teardown"):
            teardown_residue = teardown_validation_residue(
                candidate_roots=residue_roots, ports=residue_ports,
                session_identity=residue_session,
                session_manifests=residue_manifests,
            )
            teardown_residue.update({
                "revision": identity.get("commit_sha"),
                "evidence_class": "validation_process_ownership",
                "command": "teardown_validation_residue",
                "exit_code": 0 if teardown_residue.get("teardown_status") == "PASSED" else 1,
            })
            self.manifest.record("validation_residue_teardown", teardown_residue)
            if teardown_residue.get("teardown_status") != "PASSED":
                return False, "Validation residue teardown blocked"
        lifecycle_previous = dict(previous_release)
        lifecycle_previous["_published_session_id"] = self.previous_published_session_id
        source_runtime_mysql = dict(db_config or {})
        if isinstance(source_runtime_mysql.get("mysql"), dict):
            source_runtime_mysql = dict(source_runtime_mysql["mysql"])
        lifecycle_previous["_previous_runtime_mysql"] = _application_settings(
            source_runtime_mysql
        )
        lifecycle_config = dict(self._runtime_config)
        lifecycle_upgrade = dict((self._runtime_config.get("upgrade") or {}))
        if self._target_db_config:
            lifecycle_upgrade["candidate_runtime_mysql"] = _application_settings(
                self._target_db_config
            )
        lifecycle_config["upgrade"] = lifecycle_upgrade
        lifecycle = UpgradeLifecycle(
            self.repo_root, lifecycle_config, mode, lifecycle_previous
        )
        # Phase B starts with source backup and disposable-target preparation.
        # Freeze/drain is intentionally deferred until every Candidate gate has
        # passed; a failed backup or migration must not touch the active API.
        self.log("[Phase B / Step 3] Creating Pre-upgrade Full MySQL Backup & Checksum...")
        backup_db_config = dict(db_config or {"database": "coverage_tool"})
        configured_deploy_roots = list(
            backup_db_config.get("deployment_roots") or []
        )
        configured_deploy_roots.append(self.repo_root)
        for root_key in (
                "current_root", "validation_candidate_root",
                "production_candidate_root", "deployment_root"):
            if upgrade_config.get(root_key):
                configured_deploy_roots.append(upgrade_config.get(root_key))
        backup_db_config["deployment_roots"] = configured_deploy_roots
        ok_bk, bk_manifest, bk_err = perform_database_backup(
            db_config=backup_db_config,
            backup_dir=self.backup_dir,
            connection=connection,
            allow_mock_in_test=False
        )
        if not ok_bk:
            self.log(f"❌ Backup failed: {bk_err}")
            return self._fail(lifecycle, f"Backup failed: {bk_err}")
        bk_manifest["revision"] = identity.get("commit_sha")
        bk_manifest["command"] = "mysqldump --single-transaction --quick {}".format(
            (db_config or {}).get("database", "coverage_tool")
        )
        bk_manifest["exit_code"] = 0
        bk_manifest["artifact_path"] = os.path.join(
            bk_manifest.get("backup_dir", self.backup_dir), "full.sql.gz"
        )
        self.manifest.record("backup_evidence", bk_manifest)
        self.log(f"✔ Backup created & verified: SHA256={bk_manifest.get('full_sql_gz_sha256')}")

        if bk_manifest.get("evidence_class") == "mock":
            self.log("❌ Mock backup evidence cannot pass a release gate")
            return self._fail(lifecycle, "Mock backup evidence rejected")

        target_preparation = {}
        if target_connection_deferred:
            preparation_mode = str(
                upgrade_config.get("target_preparation_mode") or ""
            ).strip()
            source_generation = self._database_generation.get("generation")
            if source_generation == VNEXT and preparation_mode != \
                    RESTORE_FROM_VERIFIED_BACKUP:
                return self._fail(
                    lifecycle,
                    "Existing-VNext requires restore_from_verified_backup target preparation",
                )
            if source_generation == LEGACY and preparation_mode != EMPTY_NEW_TARGET:
                return self._fail(
                    lifecycle,
                    "Legacy migration requires empty_new_target preparation",
                )
            try:
                prepared_target = target_connection_factory(
                    bk_manifest, source_generation,
                )
                if isinstance(prepared_target, tuple) and len(prepared_target) == 2:
                    target_connection, target_preparation = prepared_target
                elif isinstance(prepared_target, dict) and \
                        "connection" in prepared_target:
                    target_connection = prepared_target.get("connection")
                    target_preparation = dict(
                        prepared_target.get("evidence") or {}
                    )
                else:
                    target_connection = prepared_target
                if target_connection is None:
                    raise RuntimeError(
                        "target_connection_factory returned no target connection"
                    )
                if not isinstance(target_preparation, dict):
                    target_preparation = {}
                if (target_preparation.get("candidate_access") or {}).get(
                        "status") != "PASSED":
                    target_preparation["candidate_access"] = \
                        probe_candidate_connection_access(
                            target_connection, self._target_db_config
                        )
                target_generation = inspect_database_generation(target_connection)
                self._target_database_generation = target_generation
                separation = validate_migration_database_separation(
                    db_config or {}, self._target_db_config,
                    source_connection=connection,
                    target_connection=target_connection,
                )
                target_generation_value = target_generation.get("generation")
                if source_generation == VNEXT and target_generation_value != VNEXT:
                    raise RuntimeError(
                        "restored Existing-VNext target is not classified as VNEXT"
                    )
                if source_generation == LEGACY and target_generation_value == LEGACY:
                    raise RuntimeError(
                        "empty Legacy migration target unexpectedly contains Legacy schema"
                    )
                target_preparation.update({
                    "status": "PASSED",
                    "source_generation": source_generation,
                    "target_generation": target_generation,
                    "database_separation": separation,
                    "command": "create disposable target + restore/empty-target probe",
                    "exit_code": 0,
                })
                self._target_preparation = dict(target_preparation)
                self.manifest.record("disposable_target", target_preparation)
                self.manifest.record("database_separation", {
                    "status": "PASSED", "revision": identity.get("commit_sha"),
                    "evidence_class": "blue_green_database",
                    "separation": separation,
                    "target_generation": target_generation,
                    "command": "validate_migration_database_separation + inspect_database_generation(target)",
                    "exit_code": 0,
                })
            except Exception as exc:
                self.log("❌ Disposable target preparation failed: {}".format(exc))
                return self._fail(lifecycle, "Disposable target preparation failed")
        elif target_connection is not None:
            # A caller may deliberately provision the target out of band.  It
            # still has to identify itself as a separate database and its
            # semantic backup consistency is checked by the migration below.
            target_preparation = {
                "status": "PASSED",
                "preparation_mode": str(
                    upgrade_config.get("target_preparation_mode") or
                    PRE_RESTORED_CONSISTENT_BACKUP
                ).strip(),
                "target_database": (self._target_db_config or {}).get("database", ""),
                "target_connection_supplied": True,
                "target_retained_for_candidate": True,
            }
            target_preparation["candidate_access"] = probe_candidate_connection_access(
                target_connection, self._target_db_config
            )
            self.manifest.record("disposable_target", target_preparation)

        # Step 4: Blue/green database migration and authoritative fact gate.
        # The source is only inspected/backed up.  Every DDL/DML operation is
        # sent to the disposable target connection.
        self.log("[Step 4/10] Migrating the disposable database target...")
        if connection is None or target_connection is None:
            return self._fail(
                lifecycle, "Source and disposable target database connections are required"
            )
        source_generation = self._database_generation.get("generation")
        try:
            if source_generation == VNEXT:
                migration_report = upgrade_existing_vnext(
                    connection, target_connection,
                    release_sha=identity.get("commit_sha", ""),
                    schema_path=ddl_path,
                )
            elif source_generation == LEGACY:
                target_schema_path = os.path.join(
                    self.repo_root, "scripts", "upgrade", "vnext_schema.sql"
                )
                core_result = apply_schema(
                    target_connection, target_schema_path,
                    release_sha=identity.get("commit_sha", ""),
                )
                migration_report = migrate_legacy(
                    connection, target_connection,
                    anomaly_path=upgrade_config.get("migration_anomaly_path") or None,
                    release_sha=identity.get("commit_sha", ""),
                )
                migration_report["target_core_schema"] = core_result
            else:
                raise RuntimeError("database generation is UNKNOWN")
            if migration_report.get("status") != "PASSED":
                raise RuntimeError(
                    "migration returned non-PASSED status: {}".format(
                        migration_report
                    )
                )
            file_state_gates = migration_report.get("file_state_ready_gate")
            if not isinstance(file_state_gates, list):
                raise RuntimeError(
                    "migration did not return an explicit FileState Ready Gate"
                )
            file_state_failed = []
            for index, gate in enumerate(file_state_gates):
                if not isinstance(gate, dict) or gate.get("status") != "PASSED":
                    file_state_failed.append("project {} status is not PASSED".format(index))
                    continue
                conditions = gate.get("explicit_conditions") or {}
                if conditions and not all(value is True for value in conditions.values()):
                    file_state_failed.append(
                        "project {} explicit FileState condition failed".format(index)
                    )
            file_state_status = "PASSED" if not file_state_failed else "FAILED"
            self.manifest.record("file_state_gate", {
                "status": file_state_status,
                "revision": identity.get("commit_sha"),
                "evidence_class": "blue_green_database",
                "database_generation": source_generation,
                "project_gates": file_state_gates,
                "project_count": len(file_state_gates),
                "conditions_passed": not file_state_failed,
                "violations": file_state_failed,
                "command": "FileStateService rebuild_validate_and_mark_ready",
                "exit_code": 0 if not file_state_failed else 1,
            })
            if file_state_failed:
                raise RuntimeError(
                    "FileState Ready Gate failed: {}".format(
                        "; ".join(file_state_failed)
                    )
                )
        except Exception as exc:
            self.log("❌ Disposable target migration failed: {}".format(exc))
            return self._fail(lifecycle, "Disposable target migration failed")
        self.manifest.record("schema_migration", {
            "preflight_safe": True, "status": "PASSED",
            "revision": identity.get("commit_sha"),
            "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
            "database_generation": source_generation,
            "source_database": (db_config or {}).get("database", ""),
            "target_database": (self._target_db_config or {}).get("database", ""),
            "migration": migration_report,
            "command": "Existing-VNext runtime-v3 or Legacy-to-VNext on disposable target",
            "exit_code": 0,
        })
        integrity = migration_report.get("authoritative_data_integrity") or {}
        if source_generation == LEGACY:
            integrity = {
                "status": "PASSED" if migration_report.get(
                    "authoritative_semantic_match"
                ) else "FAILED",
                "source_semantic_hash": migration_report.get("source_semantic_hash", ""),
                "target_semantic_hash": migration_report.get("target_semantic_hash", ""),
                "differences": migration_report.get("semantic_mismatch_components", []),
            }
        integrity_status = integrity.get("status")
        self.manifest.record("data_hash_verification", {
            "verified": integrity_status == "PASSED",
            "status": integrity_status or "FAILED",
            "evidence_class": "blue_green_database",
            "revision": identity.get("commit_sha"),
            "source_semantic_hash": integrity.get("source_semantic_hash", ""),
            "target_semantic_hash": integrity.get("target_semantic_hash", ""),
            "differences": integrity.get("differences", []),
            "source_read_only_stability": migration_report.get(
                "source_read_only_stability", {}
            ),
            "command": "authoritative VNext semantic snapshot comparison",
            "exit_code": 0 if integrity_status == "PASSED" else 1,
        })
        if integrity_status != "PASSED":
            return self._fail(lifecycle, "Authoritative data integrity verification failed")
        self.log("✔ Disposable target migration and authoritative data gate passed.")

        # Phase B publication is preparation only.  It creates an immutable
        # release directory but must not alter CURRENT or the active service.
        # The switch is performed in the short Phase D cutover window below,
        # after the isolated Candidate has passed all validation gates.
        self.log("[Phase B] Preparing immutable Candidate release (CURRENT unchanged)...")
        session_id = self.validation_session.data.get("session_id")
        offline_evidence = upgrade_config.get("offline_operator_evidence", "")
        offline_bundle = upgrade_config.get("offline_operator_source_bundle", "")
        if offline_evidence and not os.path.isabs(str(offline_evidence)):
            offline_evidence = os.path.join(self.repo_root, str(offline_evidence))
        if offline_bundle and not os.path.isabs(str(offline_bundle)):
            offline_bundle = os.path.join(self.repo_root, str(offline_bundle))
        try:
            prepared = self.publisher.prepare(
                self.production_candidate_root,
                identity,
                session_id,
                api_contract_version=upgrade_config.get("api_contract_version", ""),
                candidate_sha=identity.get("commit_sha"),
                candidate_artifact_manifest=upgrade_config.get(
                    "production_candidate_artifact_manifest", ""
                ),
                source_repo_root=self.repo_root,
                trusted_build_workflow_identity=upgrade_config.get(
                    "production_candidate_builder_workflow_identity", ""
                ),
                trusted_build_workflow_sha=upgrade_config.get(
                    "production_candidate_builder_workflow_sha", ""
                ),
                candidate_build_receipt=self.candidate_preflight.get(
                    "candidate_build_receipt", ""
                ),
                candidate_build_attestation_bundle=self.candidate_preflight.get(
                    "candidate_build_attestation_bundle", ""
                ),
                candidate_build_attestation_repository=self.candidate_preflight.get(
                    "candidate_build_attestation_repository", ""
                ),
                candidate_build_attestation_workflow=self.candidate_preflight.get(
                    "candidate_build_attestation_workflow", ""
                ),
                release_trust_mode=self._release_trust_mode,
                offline_operator_evidence=offline_evidence,
                offline_operator_source_bundle=offline_bundle,
                offline_operator_repository=upgrade_config.get(
                    "offline_operator_repository", ""
                ),
                production_host=upgrade_config.get("production_host", ""),
                production_baseline_sha=upgrade_config.get("production_baseline_sha") or
                previous_release.get("commit_sha", ""),
                validation_session_id=session_id,
            )
            candidate_manifest_summary = prepared.get("candidate_artifact_manifest") or {}
            self._candidate_artifact_sha256 = str(
                candidate_manifest_summary.get("artifact_sha256") or ""
            )
            self._served_root_sha256 = str(
                (prepared.get("served_root") or {}).get("sha256") or ""
            )
            if not self._candidate_artifact_sha256 or not self._served_root_sha256:
                raise RuntimeError("immutable publication hashes are incomplete")
            self.validation_session.data["candidate_root"] = \
                self.publisher.release_path(session_id)
            self.validation_session.save()
            application_evidence = validate_production_application_bundle(
                self.publisher.release_path(session_id)
            )
            application_evidence.update({
                "revision": identity.get("commit_sha"),
                "evidence_class": "staging_cutover" if mode == "staging" else
                "production_integration",
            })
            self.manifest.record(
                "production_application_bundle", application_evidence
            )
            self.manifest.record("candidate_release_prepared", {
                "status": "PASSED",
                "revision": identity.get("commit_sha"),
                "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
                "action_count": len(actions),
                "publication_mode": "immutable_release",
                "production_candidate_root": self.production_candidate_root,
                "validation_candidate_root": self.validation_candidate_root,
                "publish_root": self.publish_root,
                "release_root": self.publisher.release_path(session_id),
                "previous_session_id": self.previous_published_session_id,
                "current_session_id": session_id,
                "release_validation_session_id": session_id,
                "candidate_artifact_sha256": self._candidate_artifact_sha256,
                "served_root_sha256": self._served_root_sha256,
                "release_manifest": prepared,
                "current_unchanged": True,
                "command": "ImmutableReleasePublisher.prepare (no CURRENT switch)",
                "exit_code": 0,
            })
            if self._production_lifecycle_adapter is not None:
                validation_binding = \
                    self._production_lifecycle_adapter.bind_validation_candidate(
                        os.path.join(self.publisher.release_path(session_id), "app"),
                        self._target_db_config,
                    )
                validation_daemon_reload = \
                    self._production_lifecycle_adapter.daemon_reload()
                self.manifest.record("production_validation_runtime_binding", {
                    "status": "PASSED",
                    "revision": identity.get("commit_sha"),
                    "evidence_class": "production_integration",
                    "binding": validation_binding,
                    "daemon_reload": validation_daemon_reload,
                    "credentials_written_to_evidence": False,
                    "exit_code": 0,
                })
        except Exception as exc:
            self.log("❌ Immutable release publication failed: {}".format(exc))
            return self._fail(lifecycle, "Immutable release publication failed")

        try:
            start_evidence = lifecycle.start_validation_api()
            start_evidence.update({
                "revision": identity.get("commit_sha"),
                "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
                "process_role": "validation_candidate",
            })
            self.manifest.record("api_start", start_evidence)
            endpoint = ((runtime_config or {}).get("upgrade") or {}).get(
                "candidate_release_endpoint"
            ) or ((runtime_config or {}).get("upgrade") or {}).get(
                "release_endpoint"
            )
            endpoint_evidence = self._verify_release_endpoint(endpoint, identity)
            endpoint_evidence.update({
                "revision": identity.get("commit_sha"),
                "process_role": "validation_candidate",
            })
            self.manifest.record("candidate_release_endpoint", endpoint_evidence)
            if self._production_lifecycle_adapter is not None:
                ownership = self._production_lifecycle_adapter.validation_process_ownership()
                validation_ports = self.validation_session.data.get("ports") or []
                for port in validation_ports:
                    self.validation_session.add_process(
                        ownership["pid"], port=port,
                        listener={"pid": ownership["pid"], "port": int(port)},
                    )
                start_evidence["process_ownership"] = ownership
                self.manifest.record("api_start", start_evidence)
        except Exception as exc:
            self.log("❌ Candidate API verification failed: {}".format(exc))
            return self._fail(lifecycle, "Candidate API verification failed")

        # Step 5: Run Targeted Unit Test Suites
        self.log("[Step 5/10] Executing Targeted Unit Test Suites (Phases 0-6)...")
        configured_test_modules = upgrade_config.get("targeted_test_modules")
        if isinstance(configured_test_modules, str):
            configured_test_modules = [
                item.strip() for item in configured_test_modules.split(",")
                if item.strip()
            ]
        test_modules = list(configured_test_modules or (
            "tests.vnext.test_existing_vnext_upgrade",
            "tests.vnext.test_legacy_migration_contract",
            "tests.release.test_offline_operator_trust",
            "tests.release.test_current_adoption",
            "tests.upgrade.test_validation_residue",
            "tests.database.test_vnext_backup_snapshot",
            "tests.release.test_immutable_release_publication",
            "tests.release.test_production_candidate_build",
            "tests.vnext.test_runtime_config",
            "tests.release.test_upgrade_manifest",
        ))
        if not test_modules:
            return self._fail(lifecycle, "No targeted test modules configured")
        
        test_results = {}
        for tm in test_modules:
            cmd = [sys.executable, "-m", "unittest", tm]
            # Test fixtures may register temporary report directories.  Keep
            # that disposable registry separate from the live runtime
            # registry, whose reachability is audited below.
            isolated_registry = tempfile.mkdtemp(prefix="coverage-test-registry-", dir=os.path.join(self.repo_root, ".artifacts"))
            test_env = dict(os.environ)
            test_env["COVERAGE_REGISTRY_DIR"] = isolated_registry
            try:
                res = subprocess.run(cmd, cwd=self.repo_root, env=test_env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            finally:
                shutil.rmtree(isolated_registry, ignore_errors=True)
            if res.returncode != 0:
                err_text = res.stderr.decode("utf-8", errors="ignore")
                self.log(f"❌ Targeted test failed: {tm}\n{err_text}")
                return self._fail(lifecycle, f"Test {tm} failed")
            test_results[tm] = {
                "status": "PASSED", "revision": identity.get("commit_sha"),
                "evidence_class": "unit", "command": "{} -m unittest {}".format(sys.executable, tm),
                "exit_code": 0,
            }
            self.log(f"  ✔ {tm}: PASSED")
            
        self.manifest.record("targeted_tests", test_results)

        # Step 6: Run Node DOM & Event-loop Smoke Suite
        self.log("[Step 6/10] Executing Browser DOM & Event-loop Smoke Suite (5 Scenarios)...")
        b_res = subprocess.run(["node", "test_lazy_collapse_browser_smoke.js"], cwd=self.repo_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if b_res.returncode != 0:
            err_text = b_res.stderr.decode("utf-8", errors="ignore")
            self.log(f"❌ Browser smoke suite failed: {err_text}")
            return self._fail(lifecycle, "Browser smoke suite failed")
        # This Playwright suite is a fixture regression only.  It must never
        # be recorded as evidence of the externally served Candidate.
        browser_cmd = ["npm", "run", "test:browser", "--", "--reporter=line"]
        browser_fixture = subprocess.run(
            browser_cmd, cwd=self.repo_root, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        fixture_status = "PASSED" if browser_fixture.returncode == 0 else "FAILED"
        self.manifest.record("browser_fixture_regression", {
            "status": fixture_status,
            "revision": identity.get("commit_sha"),
            "evidence_class": "browser_fixture_regression",
            "synthetic": True,
            "suite": "tests/browser/coverage_real_browser.spec.js",
            "command": " ".join(browser_cmd),
            "exit_code": browser_fixture.returncode,
        })
        if browser_fixture.returncode != 0:
            err_text = browser_fixture.stderr.decode("utf-8", errors="ignore")
            self.log("❌ Browser fixture regression failed: {}".format(err_text))
            return self._fail(lifecycle, "Browser fixture regression failed")
        self.log("✔ Browser fixture regression passed.")

        # Production authority is an external artifact generated by
        # real_browser_evidence.js against the actual Candidate URL.  The
        # fixture command above is intentionally not consulted for this gate.
        self.log("[Step 6b/10] Validating external real Candidate browser evidence...")
        browser_evidence_path = self.candidate_browser_evidence_path
        expected_browser_url = str(upgrade_config.get("candidate_browser_url") or "")
        browser_payload = {}
        browser_errors = []
        if not browser_evidence_path or not os.path.isfile(browser_evidence_path):
            browser_errors.append(
                "upgrade.candidate_browser_evidence_path must point to real Candidate evidence"
            )
        else:
            try:
                with open(browser_evidence_path, "r", encoding="utf-8") as browser_stream:
                    browser_payload = json.load(browser_stream)
            except (OSError, ValueError, TypeError) as exc:
                browser_errors.append("Candidate browser evidence is unreadable: {}".format(exc))
        if not browser_errors:
            browser_errors, normalized_browser = _validate_candidate_browser_evidence(
                browser_evidence_path, browser_payload, identity, expected_browser_url,
                expected_session_id=self.release_validation_session_id,
                expected_candidate_artifact_sha256=self._candidate_artifact_sha256,
                expected_served_root_sha256=self._served_root_sha256,
            )
        else:
            normalized_browser = {
                "status": "FAILED",
                "evidence_class": "real_candidate_browser",
                "candidate_url": expected_browser_url,
                "expected_commit_sha": identity.get("commit_sha"),
                "release_validation_session_id": self.release_validation_session_id,
                "candidate_artifact_sha256": self._candidate_artifact_sha256,
                "served_root_sha256": self._served_root_sha256,
                "served_release_identity": {},
                "browser_artifact_path": "",
                "browser_artifact_sha256": "",
                "real_http": False,
                "chromium": False,
                "synthetic": False,
                "release_eligible": False,
                "violations": browser_errors,
            }
        normalized_browser.update({
            "revision": identity.get("commit_sha"),
            "artifact_path": browser_evidence_path,
            "command": "validate real_browser_evidence.js --browser-evidence-output",
            "exit_code": 0 if not browser_errors else 1,
        })
        if browser_evidence_path and os.path.isfile(browser_evidence_path):
            normalized_browser["evidence_artifact_sha256"] = _sha256_file(browser_evidence_path)
        self.manifest.record("candidate_browser_evidence", normalized_browser)
        if browser_errors:
            self.log("❌ Real Candidate browser evidence rejected: {}".format(
                "; ".join(browser_errors)
            ))
            return self._fail(lifecycle, "Real Candidate browser evidence rejected")
        self.log("✔ Real Candidate browser evidence passed.")

        # Step 7: Consume immutable release A/B evidence.  The synthetic DOM
        # benchmark remains a useful diagnostic, but it is not allowed to
        # create a production performance claim inside the upgrade runner.
        self.log("[Step 7/10] Validating exact-revision Performance A/B Evidence...")
        perf_artifact = self.performance_evidence_path
        perf_cmd = "validate release_performance_ab artifact {}".format(perf_artifact or "<missing>")
        perf_res = {}
        perf_errors = []
        if not perf_artifact or not os.path.isfile(perf_artifact):
            perf_errors.append("upgrade.performance_evidence_path must point to a release A/B artifact")
        else:
            try:
                with open(perf_artifact, "r", encoding="utf-8") as perf_stream:
                    perf_res = json.load(perf_stream)
            except (OSError, ValueError) as exc:
                perf_errors.append("release performance artifact is unreadable: {}".format(exc))
        if not perf_errors:
            perf_errors.extend(_validate_release_performance_artifact(
                perf_artifact, perf_res, identity.get("commit_sha"),
                expected_session_id=self.release_validation_session_id,
                expected_candidate_artifact_sha256=self._candidate_artifact_sha256,
                expected_served_root_sha256=self._served_root_sha256,
            ))
        if perf_errors:
            invalid_perf = dict(perf_res) if isinstance(perf_res, dict) else {}
            invalid_perf.update({
                "status": "FAILED",
                "evidence_class": "release_performance_ab",
                "revision": identity.get("commit_sha"),
                "command": perf_cmd,
                "exit_code": 1,
                "artifact_path": perf_artifact,
                "violations": perf_errors,
            })
            self.manifest.record("performance_benchmark", invalid_perf)
            self.log("❌ Release performance A/B evidence rejected: {}".format("; ".join(perf_errors)))
            return self._fail(lifecycle, "Release performance A/B evidence rejected")

        perf_res["revision"] = identity.get("commit_sha")
        perf_res["command"] = perf_cmd
        perf_res["exit_code"] = 0
        perf_res["artifact_path"] = perf_artifact
        for tier_name in ("Tier_A_1k", "Tier_B_10k", "Tier_C_50k", "Tier_D_100k"):
            tier = perf_res.get(tier_name)
            if isinstance(tier, dict):
                tier["revision"] = identity.get("commit_sha")
                tier["evidence_class"] = "release_performance_ab"
                tier["command"] = perf_cmd
                tier["exit_code"] = 0
        self.manifest.record("performance_benchmark", perf_res)
        self.log(
            "  ✔ Tier A: baseline={}ms/candidate={}ms, Tier D (100k): baseline={}ms/candidate={}ms".format(
                perf_res["Tier_A_1k"].get("baseline_ms"),
                perf_res["Tier_A_1k"].get("candidate_ms"),
                perf_res["Tier_D_100k"].get("baseline_ms"),
                perf_res["Tier_D_100k"].get("candidate_ms"),
            )
        )

        # Step 8: Run Path Mapping & Sidecar Audits
        self.log("[Step 8/10] Running Path Mapping & Sidecar Integrity Audits...")
        known = []
        configured_sources = (((runtime_config or {}).get("upgrade") or {}).get("path_mapping_source_paths") or [])
        source_roots = [self.repo_root] if not configured_sources else [
            os.path.realpath(str(p) if os.path.isabs(str(p)) else os.path.join(self.repo_root, str(p)))
            for p in configured_sources
        ]
        for source_root in source_roots:
            if not os.path.exists(source_root):
                raise RuntimeError("configured path-mapping source does not exist: {}".format(source_root))
            if os.path.isfile(source_root):
                candidates = [source_root]
            else:
                candidates = []
                for root, dirs, files in os.walk(source_root):
                    dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", ".artifacts")]
                    candidates.extend(os.path.join(root, name) for name in files)
            for candidate in candidates:
                if os.path.splitext(candidate)[1].lower() in (".c", ".h", ".cc", ".cpp", ".hpp"):
                    if source_root == self.repo_root:
                        known.append(os.path.relpath(candidate, self.repo_root).replace(os.sep, "/"))
                    else:
                        known.append(os.path.relpath(candidate, source_root).replace(os.sep, "/"))
        known = sorted(set(known))
        queries = []
        if known:
            first = known[0]
            queries.append((first, "exact"))
            # LCOV/external path identities containing parent traversal are
            # rejected by the canonical resolver rather than folded into a
            # different path.
            queries.append(("../" + first, "invalid_path"))
            queries.append((os.path.basename(first), "basename_only_rejected"))
            queries.append(("__missing__/not-present.c", "miss"))
        p_audit = audit_path_mappings(known, queries)
        if not known:
            p_audit.update({"is_valid": False, "violations": ["No repository source paths were available for audit"]})
        lcov_paths = (((runtime_config or {}).get("upgrade") or {}).get("path_mapping_lcov_paths") or [])
        lcov_files = [
            os.path.realpath(str(path) if os.path.isabs(str(path)) else os.path.join(self.repo_root, str(path)))
            for path in lcov_paths
        ]
        if lcov_files:
            if any(not os.path.isfile(path) for path in lcov_files):
                p_audit.update({
                    "is_valid": False,
                    "violations": list(p_audit.get("violations", [])) + ["Configured LCOV audit input is missing"],
                    "input_kind": "repository_lcov",
                })
            else:
                lcov_audit = audit_lcov_paths(known, lcov_files)
                p_audit.update(lcov_audit)
                p_audit["input_kind"] = "repository_lcov"
                p_audit["is_valid"] = bool(p_audit.get("is_valid") and lcov_audit.get("is_valid"))
                p_audit["violations"] = list(p_audit.get("violations", [])) + list(lcov_audit.get("violations", []))
        else:
            p_audit["input_kind"] = "repository_paths_only"
        p_audit.update({
            "status": "PASSED" if p_audit.get("is_valid") else "FAILED",
            "revision": identity.get("commit_sha"),
            "evidence_class": "integration",
            "command": "audit_path_mappings (repository/LCOV path inventory)",
            "exit_code": 0 if p_audit.get("is_valid") else 1,
        })
        self.manifest.record("path_mapping_audit", p_audit)
        self.log(f"  ✔ Path Mapping Audit: is_valid={p_audit['is_valid']}")

        s_audit = audit_sidecar_and_registry([self.repo_root, "/opt/coverage_tool"])
        s_audit.update({
            "status": "PASSED" if s_audit.get("is_safe") else "FAILED",
            "revision": identity.get("commit_sha"),
            "evidence_class": "integration",
            "command": "audit_sidecar_and_registry",
            "exit_code": 0 if s_audit.get("is_safe") else 1,
        })
        self.manifest.record("sidecar_audit", s_audit)
        if not s_audit["is_safe"]:
            self.log(f"❌ Sidecar audit failed: {s_audit}")
            return self._fail(lifecycle, "Sidecar audit safety violations")
        self.log(f"  ✔ Sidecar & Registry Audit: is_safe={s_audit['is_safe']}")

        # Step 9: Security Vulnerability Scanner
        self.log("[Step 9/10] Running Static Security Vulnerability Scanner...")
        sec_res = scan_directory(self.repo_root)
        self.manifest.record("security_audit", {
            "scanned_files": sec_res["scanned_files"],
            "critical_count": sec_res["critical_count"],
            "high_count": sec_res["high_count"],
            "is_safe": sec_res["is_safe"]
            ,"status": "PASSED" if sec_res["is_safe"] else "FAILED"
            ,"revision": identity.get("commit_sha")
            ,"evidence_class": "integration"
            ,"auth_mode": (db_config or {}).get("auth_mode", "reverse_proxy")
            ,"command": "scan_directory"
            ,"exit_code": 0 if sec_res["is_safe"] else 1
        })
        if not sec_res["is_safe"]:
            self.log(f"❌ Security audit failed: {sec_res}")
            return self._fail(lifecycle, "Security audit unresolved findings")
        self.log(f"  ✔ Security Scan passed: Critical={sec_res['critical_count']}, High={sec_res['high_count']}")

        rollback_path = self.rollback_evidence_path
        rollback_evidence = {}
        if rollback_path:
            try:
                with open(rollback_path, "r", encoding="utf-8") as stream:
                    rollback_evidence = json.load(stream)
                if rollback_evidence.get("revision") != identity.get("commit_sha"):
                    raise RuntimeError("rollback evidence revision mismatch")
                if rollback_evidence.get("release_validation_session_id") != \
                        self.release_validation_session_id:
                    raise RuntimeError("rollback evidence attempt identity mismatch")
                before_id = rollback_evidence.get("before_release_id")
                target_id = rollback_evidence.get("target_release_id")
                rollback_id = rollback_evidence.get("rollback_release_id")
                if not before_id or not target_id or not rollback_id:
                    raise RuntimeError("rollback evidence lacks release identities")
                if target_id != self.release_validation_session_id:
                    raise RuntimeError("rollback evidence target is not the current attempt")
                if before_id == target_id or rollback_id != before_id:
                    raise RuntimeError("rollback evidence does not restore the before release")
                if rollback_evidence.get("candidate_artifact_sha256") != \
                        self._candidate_artifact_sha256:
                    raise RuntimeError("rollback evidence Candidate artifact hash mismatch")
                if rollback_evidence.get("served_root_sha256") != \
                        self._served_root_sha256:
                    raise RuntimeError("rollback evidence Served Root hash mismatch")
                rollback_evidence.setdefault("evidence_class", "staging_cutover" if mode == "staging" else "production_cutover")
                rollback_evidence.setdefault("command", "run_rollback_rehearsal")
                rollback_evidence.setdefault("exit_code", 0 if rollback_evidence.get("status") == "PASSED" else 1)
                rollback_evidence.setdefault("artifact_path", os.path.abspath(rollback_path))
                self.manifest.record("rollback_evidence", rollback_evidence)
            except Exception as exc:
                invalid_rollback = dict(rollback_evidence) if isinstance(
                    rollback_evidence, dict
                ) else {}
                invalid_rollback.update({
                    "status": "FAILED",
                    "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
                    "revision": identity.get("commit_sha"),
                    "release_validation_session_id": self.release_validation_session_id,
                    "candidate_artifact_sha256": self._candidate_artifact_sha256,
                    "served_root_sha256": self._served_root_sha256,
                    "violations": [str(exc)],
                    "command": "validate rollback rehearsal evidence",
                    "exit_code": 1,
                    "artifact_path": os.path.abspath(rollback_path),
                })
                self.manifest.record("rollback_evidence", invalid_rollback)
                self.log("❌ Rollback rehearsal evidence unavailable: {}".format(exc))
        else:
            self.manifest.record("rollback_evidence", {
                "status": "FAILED",
                "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
                "revision": identity.get("commit_sha"),
                "release_validation_session_id": self.release_validation_session_id,
                "candidate_artifact_sha256": self._candidate_artifact_sha256,
                "served_root_sha256": self._served_root_sha256,
                "violations": ["rollback evidence artifact path is missing"],
                "command": "validate rollback rehearsal evidence",
                "exit_code": 1,
                "artifact_path": "",
            })
            self.log("❌ Rollback rehearsal evidence unavailable: artifact path is missing")

        # Teardown is a hard release prerequisite.  This runs before the
        # final evidence read and records both the session manifest and the
        # owned-process/port closure result.
        teardown = self._teardown_validation_session(
            identity, mode, lifecycle=lifecycle
        )
        if not teardown or teardown.get("status") != "PASSED":
            self.log("❌ Release NOT_READY: validation session teardown is incomplete.")
            return self._fail(lifecycle, "Validation session teardown failed")

        ready, unmet = self._validate_pre_cutover_ready(identity, mode)
        if not ready:
            self.log(
                "❌ Release NOT_READY: PRE_CUTOVER_READY hard gate failed: {}".format(
                    "; ".join(unmet)
                )
            )
            return self._fail(lifecycle, "PRE_CUTOVER_READY hard gate failed")
        lifecycle.api_started = False

        # Phase D is the only cutover window.  Candidate validation, browser,
        # performance, audits, and rollback rehearsal have all completed
        # while CURRENT and the active API were untouched.
        self.log("[Phase D] Candidate is PRE_CUTOVER_READY; entering short cutover window...")
        try:
            freeze_evidence = lifecycle.freeze(identity.get("commit_sha", ""))
            freeze_evidence["revision"] = identity.get("commit_sha")
            self.manifest.record("traffic_freeze", freeze_evidence)
            drain_timeout = float(
                ((runtime_config or {}).get("upgrade") or {}).get(
                    "drain_timeout_sec", 30
                )
            )
            drain_evidence = lifecycle.drain(
                connection, timeout_sec=drain_timeout
            )
            drain_evidence["revision"] = identity.get("commit_sha")
            self.manifest.record("job_drain", drain_evidence)

            cutover_identity = self._reconfirm_cutover_identity(
                connection, target_connection, db_config,
                self._target_db_config, previous_release,
            )
            cutover_identity.update({
                "revision": identity.get("commit_sha"),
                "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
            })
            self.manifest.record("cutover_identity_reconfirmation", cutover_identity)

            if self._production_lifecycle_adapter is not None:
                lifecycle.current_serving_managed = lifecycle._has_active_current_serving_state()
                lifecycle.current_api_stop_attempted = True
                stop_evidence = self._production_lifecycle_adapter.stop_service()
                lifecycle.current_api_stopped = True
            else:
                stop_evidence = lifecycle.stop_current_api()
            stop_evidence.update({
                "revision": identity.get("commit_sha"),
                "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
                "process_role": "pre_upgrade_baseline",
            })
            self.manifest.record("api_stop", stop_evidence)
        except Exception as exc:
            self.log("❌ Cutover freeze/drain/stop failed: {}".format(exc))
            return self._fail(lifecycle, "Cutover freeze/drain/stop failed")

        # Flat adoption is deliberately inside Phase D.  It is the first
        # operation allowed to create CURRENT for a historical Flat install.
        self.log("[Phase D] Adopting Flat baseline if required and switching CURRENT atomically...")
        try:
            adoption = self._ensure_flat_current_baseline(
                upgrade_config, previous_release
            )
            # UpgradeLifecycle snapshots the rollback identity at construction
            # time.  Flat adoption creates the immutable baseline session only
            # at this boundary, so publish that generated session into the
            # lifecycle before any later gate can invoke abort().
            if adoption and self.previous_published_session_id:
                lifecycle.previous_release["_published_session_id"] = \
                    self.previous_published_session_id
            switched = self.publisher.switch_current(session_id)
            if switched.get("status") != "PASSED":
                raise RuntimeError("immutable CURRENT switch did not pass")
            self._publication_switched = True
            current_after_switch = self.publisher.validate_current()
            if current_after_switch.get("status") != "PASSED":
                raise RuntimeError("CURRENT failed validation after atomic switch")
            production_runtime = {}
            if self._production_lifecycle_adapter is not None:
                release_bindings = self._production_lifecycle_adapter.bind_current_release()
                self._production_release_bindings_changed = bool(
                    self._production_lifecycle_adapter.release_bindings_changed
                )
                binding = self._production_lifecycle_adapter.bind_candidate_database(
                    self._target_db_config
                )
                self._production_runtime_bound = True
                daemon_reload = self._production_lifecycle_adapter.daemon_reload()
                production_runtime = {
                    "release_bindings": release_bindings,
                    "binding": binding,
                    "daemon_reload": daemon_reload,
                }
                self.manifest.record("production_runtime_binding", {
                    "status": "PASSED",
                    "revision": identity.get("commit_sha"),
                    "evidence_class": "production_integration",
                    "release_bindings": release_bindings,
                    "binding": binding,
                    "daemon_reload": daemon_reload,
                    "credentials_written_to_evidence": False,
                    "exit_code": 0,
                })
            self.manifest.record("file_cutover", {
                "status": "PASSED",
                "revision": identity.get("commit_sha"),
                "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
                "action_count": len(actions),
                "publication_mode": "immutable_release",
                "production_candidate_root": self.production_candidate_root,
                "validation_candidate_root": self.validation_candidate_root,
                "publish_root": self.publish_root,
                "release_root": self.publisher.release_path(session_id),
                "previous_session_id": self.previous_published_session_id,
                "current_session_id": session_id,
                "release_validation_session_id": session_id,
                "candidate_artifact_sha256": self._candidate_artifact_sha256,
                "served_root_sha256": self._served_root_sha256,
                "release_manifest": prepared,
                "switch": switched,
                "current_validation": current_after_switch,
                "production_runtime": production_runtime,
                "command": "ImmutableReleasePublisher.switch_current",
                "exit_code": 0,
            })
        except Exception as exc:
            self.log("❌ Immutable release cutover failed: {}".format(exc))
            return self._fail(lifecycle, "Immutable release cutover failed")

        # The validation candidate is now gone by construction.  Start the
        # final serving process under a different session/command so a later
        # validation teardown can never kill the process that will receive
        # production traffic.
        lifecycle.api_started = False
        try:
            self._activate_final_serving_session()
            if self._production_lifecycle_adapter is not None:
                # The production adapter binds the persistent EnvironmentFile
                # and restarts the real systemd unit.  The generic staging
                # command remains available for non-production runs.
                lifecycle.serving_api_started = True
                serving_start = self._production_lifecycle_adapter.restart_service()
                nginx_reload = self._production_lifecycle_adapter.reload_nginx()
                self.manifest.record("production_nginx_reload", {
                    "status": "PASSED",
                    "revision": identity.get("commit_sha"),
                    "evidence_class": "production_integration",
                    "nginx": nginx_reload,
                    "credentials_written_to_evidence": False,
                    "exit_code": 0,
                })
            else:
                serving_start = lifecycle.start_serving_api()
            serving_start.update({
                "revision": identity.get("commit_sha"),
                "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
                "process_role": "production_serving",
                "serving_session_id": self.serving_session_id,
                "serving_session_manifest": self.serving_session_manifest_path,
            })
            # api_start and release_endpoint represent the process that will
            # be served, rather than the already-torn-down validation process.
            self.manifest.record("api_start", serving_start)
            endpoint = ((runtime_config or {}).get("upgrade") or {}).get("release_endpoint")
            endpoint_evidence = self._verify_release_endpoint(endpoint, identity)
            endpoint_evidence.update({
                "revision": identity.get("commit_sha"),
                "process_role": "production_serving",
            })
            self.manifest.record("release_endpoint", endpoint_evidence)
        except Exception as exc:
            self.log("❌ Final serving API verification failed: {}".format(exc))
            return self._fail(lifecycle, "Final serving API verification failed")

        # Step 10: Validate Final Production Release Governance Gate
        self.log("[Step 10/10] Validating Production Release Governance Gate...")
        gate_passed, unmet = self.manifest.validate_final_gate(
            require_traffic_open=False, require_post_open_serving=False
        )
        if not gate_passed:
            return self._fail(lifecycle, f"Final gate unmet: {unmet}")

        try:
            open_evidence = lifecycle.open_traffic()
            open_evidence.update({
                "revision": identity.get("commit_sha"),
                "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
            })
            self.manifest.record("traffic_open", open_evidence)
            post_open = self._verify_post_open_serving(identity)
            self.manifest.record("post_open_serving", post_open)
            final_open_gate, open_unmet = self.manifest.validate_final_gate(
                require_traffic_open=True, require_post_open_serving=True
            )
            if not final_open_gate:
                self.log("❌ Post-open evidence gate failed: {}".format(open_unmet))
                return self._fail(lifecycle, "Post-open evidence gate failed")
            finalized = lifecycle.finalize_traffic_open()
            open_evidence["finalize"] = finalized
            self.manifest.record("traffic_open", open_evidence)
        except Exception as exc:
            self.manifest.record("post_open_serving", {
                "status": "FAILED",
                "revision": identity.get("commit_sha"),
                "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
                "process_role": "production_serving",
                "command": "GET release + GET health + HTTP Served Root byte identity + ImmutableReleasePublisher.validate_current",
                "exit_code": 1,
                "violations": [str(exc)],
            })
            self.log("❌ Traffic open failed; keeping writes frozen: {}".format(exc))
            return self._fail(lifecycle, "Traffic open failed")

        self.log("=== Upgrade Verification Completed Successfully ===")
        self.log("Release gate passed: all required evidence is authentic and exact.")
        return True, "RELEASE_GATE_PASSED"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manifest-driven safe coverage upgrade")
    parser.add_argument("--mode", choices=("staging", "production"), required=True)
    parser.add_argument("--manifest", required=True, dest="deployment_manifest")
    parser.add_argument("--target-release", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    with open(args.target_release, "r", encoding="utf-8") as stream:
        target = json.load(stream)
    with open(args.config, "r", encoding="utf-8") as stream:
        config = json.load(stream)
    orchestrator = UpgradeOrchestrator(
        backup_root=(config.get("upgrade") or {}).get("backup_root")
    )
    mysql_config = config.get("mysql", config)
    upgrade_config = config.get("upgrade") or {}
    target_mysql = upgrade_config.get("target_mysql") or config.get("target_mysql")
    if not isinstance(target_mysql, dict) or not target_mysql.get("database"):
        print(
            "Live upgrade requires an explicit disposable upgrade.target_mysql database",
            file=sys.stderr,
        )
        sys.exit(1)
    connection = None
    target_connection = None
    target_connection_holder = {}
    target_connection_factory = None
    target_preparation_mode = str(
        upgrade_config.get("target_preparation_mode") or ""
    ).strip()
    if target_preparation_mode not in DISPOSABLE_TARGET_MODES:
        print(
            "Live upgrade requires an explicit disposable target_preparation_mode",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        connection = connect_live_database(mysql_config)
        if target_preparation_mode in (
                RESTORE_FROM_VERIFIED_BACKUP, EMPTY_NEW_TARGET):
            def _prepare_target(backup_manifest, source_generation):
                if source_generation == VNEXT and \
                        target_preparation_mode != RESTORE_FROM_VERIFIED_BACKUP:
                    raise RuntimeError(
                        "Existing-VNext target preparation mode is invalid"
                    )
                if source_generation == LEGACY and \
                        target_preparation_mode != EMPTY_NEW_TARGET:
                    raise RuntimeError(
                        "Legacy target preparation mode is invalid"
                    )
                if target_preparation_mode == RESTORE_FROM_VERIFIED_BACKUP:
                    evidence = create_disposable_target_from_backup(
                        backup_manifest, mysql_config, target_mysql,
                    )
                else:
                    evidence = create_empty_disposable_target(
                        mysql_config, target_mysql,
                    )
                prepared_connection = connect_live_database(target_mysql)
                target_connection_holder["connection"] = prepared_connection
                return prepared_connection, evidence
            target_connection_factory = _prepare_target
        else:
            target_connection = connect_live_database(target_mysql)
        success, status = orchestrator.execute_upgrade(
            dry_run=False, mode=args.mode, connection=connection,
            db_config=dict(mysql_config, auth_mode=(config.get("auth") or {}).get("mode", "reverse_proxy")),
            deployment_manifest=args.deployment_manifest, target_release=target,
            runtime_config=config,
            target_connection=target_connection,
            target_db_config=dict(target_mysql),
            target_connection_factory=target_connection_factory,
        )
    except Exception as exc:
        print("Live upgrade connection/orchestration failed: {}".format(exc), file=sys.stderr)
        success, status = False, "LIVE_UPGRADE_FAILED"
    finally:
        target_to_close = target_connection or target_connection_holder.get("connection")
        if target_to_close is not None:
            try:
                target_to_close.close()
            except Exception:
                pass
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
    sys.exit(0 if success else 1)
