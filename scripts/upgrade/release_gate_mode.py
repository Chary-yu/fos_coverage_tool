"""Deterministic release-gate classification for Legacy/VNext compatibility.

This module is the single source of truth for deciding whether Path Mapping
and VNext-report identity are required, explicitly deferred, or blocking.
It is Python 3.6 compatible and can read the current Scan/report identity from
SQLite-compatible tests or the production PyMySQL connection.
"""
from __future__ import print_function

import re

from app.db.repositories.base import fetchall, fetchone


LEGACY_REPORT_COMPATIBLE = "LEGACY_REPORT_COMPATIBLE"
FULL_VNEXT = "FULL_VNEXT"
LEGACY_STATIC = "LEGACY_STATIC"
VNEXT_ARTIFACT_READY = "VNEXT_ARTIFACT_READY"

PATH_MAPPING_REQUIRED = "REQUIRED_STRICT"
PATH_MAPPING_DEFERRED = "DEFERRED_LEGACY_IDENTITY_GAP"
VNEXT_REPORT_REQUIRED = "REQUIRED"
VNEXT_REPORT_DEFERRED = "DEFERRED_LEGACY_REPORT_MODE"

BLOCKED_IDENTITY_INCOMPLETE = "BLOCKED_IDENTITY_INCOMPLETE"
BLOCKED_LEGACY_MODE_NOT_DECLARED = "BLOCKED_LEGACY_MODE_NOT_DECLARED"
BLOCKED_REPORT_MODE_UNKNOWN = "BLOCKED_REPORT_MODE_UNKNOWN"
BLOCKED_LINEAGE_CONFLICT = "BLOCKED_LINEAGE_CONFLICT"

_SHA40_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

CLASSIFICATION_FIELDS = (
    "scan_type",
    "legacy_migrated",
    "report_mode",
    "release_mode",
    "authoritative_lcov_repository_identity",
    "claims_vnext_code_detail",
    "path_mapping_gate",
    "vnext_report_gate",
    "report_identity_fabricated",
    "blocked",
)


def _truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    return str(value or "").strip().lower() in (
        "1", "true", "yes", "y", "on"
    )


def _canonical_mode(value):
    return str(value or "").strip().upper()


def _authoritative_lcov_identity(payload):
    info_name = str(payload.get("info_file_name") or "").strip()
    info_sha = str(payload.get("info_sha256") or "").strip()
    repo_ok = _truthy(payload.get("repository_identity_complete"))
    return bool(info_name and _SHA256_RE.match(info_sha) and repo_ok)


def classify_release_gate_mode(payload):
    """Return one deterministic Path Mapping/VNext report gate decision."""
    payload = dict(payload or {})
    scan_type = str(payload.get("scan_type") or "").strip().lower()
    legacy_flag = _truthy(payload.get("legacy_migrated"))
    legacy = legacy_flag or scan_type == "legacy_migrated"
    report_mode = _canonical_mode(payload.get("report_mode"))
    release_mode = _canonical_mode(payload.get("release_mode"))
    claims_vnext = _truthy(payload.get("claims_vnext_code_detail")) or \
        report_mode == VNEXT_ARTIFACT_READY
    authoritative = _authoritative_lcov_identity(payload)
    report_mode_conflict = _truthy(payload.get("report_mode_conflict"))

    if report_mode_conflict:
        path_gate = BLOCKED_LINEAGE_CONFLICT
        vnext_gate = BLOCKED_LINEAGE_CONFLICT
    else:
        if report_mode == VNEXT_ARTIFACT_READY or claims_vnext:
            vnext_gate = VNEXT_REPORT_REQUIRED
        elif report_mode == LEGACY_STATIC:
            if release_mode == LEGACY_REPORT_COMPATIBLE:
                vnext_gate = VNEXT_REPORT_DEFERRED
            else:
                vnext_gate = BLOCKED_LEGACY_MODE_NOT_DECLARED
        else:
            vnext_gate = BLOCKED_REPORT_MODE_UNKNOWN

        if authoritative:
            path_gate = PATH_MAPPING_REQUIRED
        elif legacy and report_mode == LEGACY_STATIC and not claims_vnext:
            if release_mode == LEGACY_REPORT_COMPATIBLE:
                path_gate = PATH_MAPPING_DEFERRED
            else:
                path_gate = BLOCKED_LEGACY_MODE_NOT_DECLARED
        else:
            path_gate = BLOCKED_IDENTITY_INCOMPLETE

    blocked = path_gate.startswith("BLOCKED_") or \
        vnext_gate.startswith("BLOCKED_")
    return {
        "scan_type": scan_type or "unknown",
        "legacy_migrated": bool(legacy),
        "report_mode": report_mode or "UNKNOWN",
        "release_mode": release_mode or "UNKNOWN",
        "authoritative_lcov_repository_identity": bool(authoritative),
        "claims_vnext_code_detail": bool(claims_vnext),
        "path_mapping_gate": path_gate,
        "vnext_report_gate": vnext_gate,
        "report_identity_fabricated": False,
        "blocked": bool(blocked),
    }


def classification_signature(payload):
    value = dict(payload or {})
    return {field: value.get(field) for field in CLASSIFICATION_FIELDS}


def compare_expected_classification(expected, actual):
    """Return mismatch errors; expected must contain the full semantic signature."""
    expected = dict(expected or {})
    actual = classification_signature(actual)
    errors = []
    for field in CLASSIFICATION_FIELDS:
        if field not in expected:
            errors.append("expected release-gate classification is missing {}".format(field))
            continue
        if expected.get(field) != actual.get(field):
            errors.append(
                "release-gate classification mismatch {}: expected {!r}, got {!r}".format(
                    field, expected.get(field), actual.get(field)
                )
            )
    return errors


def validate_classification_record(record, require_expected_binding=False,
                                   required_stage=""):
    """Validate a recorded classification without converting DEFERRED to PASS."""
    record = dict(record or {})
    errors = []
    if record.get("status") != "PASSED":
        errors.append("release gate classification record is not PASSED")
    if record.get("report_identity_fabricated") is not False:
        errors.append("report_identity_fabricated must be false")
    if record.get("blocked") is not False:
        errors.append("release gate classification is blocked")
    path_gate = str(record.get("path_mapping_gate") or "")
    report_gate = str(record.get("vnext_report_gate") or "")
    if path_gate not in (PATH_MAPPING_REQUIRED, PATH_MAPPING_DEFERRED):
        errors.append("path mapping gate is not a non-blocking classified value")
    if report_gate not in (VNEXT_REPORT_REQUIRED, VNEXT_REPORT_DEFERRED):
        errors.append("VNext report gate is not a non-blocking classified value")
    if require_expected_binding:
        expected = record.get("expected_classification")
        if not isinstance(expected, dict):
            errors.append("expected release-gate classification binding is missing")
        else:
            errors.extend(compare_expected_classification(expected, record))
        if record.get("classification_matches_expected") is not True:
            errors.append("release gate classification does not match reviewed expectation")
    if required_stage and record.get("classification_stage") != required_stage:
        errors.append(
            "release gate classification stage is not {}".format(required_stage)
        )
    return errors


def validate_vnext_report_evidence(classification, candidate_release_prepared):
    """Validate report-mode evidence against the VNext report gate decision."""
    classification = dict(classification or {})
    prepared = dict(candidate_release_prepared or {})
    errors = []
    gate = classification.get("vnext_report_gate")
    manifest = prepared.get("release_manifest")
    report_modes = manifest.get("report_modes") if isinstance(manifest, dict) else None
    if not isinstance(report_modes, list):
        report_modes = []
    normalized = sorted(set(_canonical_mode(item) for item in report_modes if item))
    if gate == VNEXT_REPORT_REQUIRED:
        if VNEXT_ARTIFACT_READY not in normalized:
            errors.append(
                "required VNext report gate lacks VNEXT_ARTIFACT_READY release evidence"
            )
    elif gate == VNEXT_REPORT_DEFERRED:
        if normalized != [LEGACY_STATIC]:
            errors.append(
                "deferred VNext report gate requires an explicit LEGACY_STATIC release"
            )
        if classification.get("report_identity_fabricated") is not False:
            errors.append("deferred VNext report gate fabricated report identity")
    else:
        errors.append("VNext report gate is blocked or unknown")
    return errors


def validate_path_mapping_evidence(classification, evidence):
    """Validate Path Mapping evidence against the canonical classification."""
    classification = dict(classification or {})
    evidence = dict(evidence or {})
    errors = validate_classification_record(classification)
    gate = classification.get("path_mapping_gate")
    if gate == PATH_MAPPING_REQUIRED:
        if evidence.get("status") != "PASSED":
            errors.append("strict path mapping evidence is not PASSED")
        if evidence.get("gate_status") != PATH_MAPPING_REQUIRED:
            errors.append("strict path mapping gate_status is not REQUIRED_STRICT")
        if evidence.get("is_valid") is not True:
            errors.append("strict path mapping evidence is not valid")
        if evidence.get("input_kind") != "repository_lcov":
            errors.append("strict path mapping evidence lacks repository+LCOV input")
    elif gate == PATH_MAPPING_DEFERRED:
        if evidence.get("status") != "DEFERRED":
            errors.append("Legacy-compatible path mapping evidence is not DEFERRED")
        if evidence.get("gate_status") != PATH_MAPPING_DEFERRED:
            errors.append("Legacy-compatible path mapping deferment reason is not exact")
        if evidence.get("is_valid") is not False:
            errors.append("deferred path mapping must not claim is_valid=true")
        if evidence.get("report_identity_fabricated") is not False:
            errors.append("deferred path mapping fabricated report identity")
        if evidence.get("input_kind") != "legacy_identity_gap":
            errors.append("deferred path mapping input_kind is not legacy_identity_gap")
    else:
        errors.append("path mapping gate is blocked or unknown")
    return errors


def _repository_identity_complete(rows):
    rows = list(rows or [])
    if not rows:
        return False
    for row in rows:
        row = dict(row or {})
        if not str(row.get("repository_name") or "").strip():
            return False
        if not _truthy(row.get("identity_verified")):
            return False
        if not _SHA40_RE.match(str(row.get("commit_sha") or "").strip()):
            return False
        if not str(row.get("identity_provenance") or "").strip():
            return False
    return True


def collect_release_gate_inputs(connection, project_name, release_mode,
                                claims_vnext_code_detail):
    """Read the currently published Scan/report identity from the source DB."""
    project_name = str(project_name or "").strip()
    if not project_name:
        raise ValueError("release gate project_name is required")
    scan = fetchone(connection, """
        SELECT s.id, s.scan_type, s.status, s.legacy_migrated,
               s.info_file_name, s.info_sha256, s.imported_at
        FROM coverage_scans s
        JOIN coverage_projects p ON p.id=s.project_id
        WHERE p.project_name=?
          AND LOWER(s.status) IN ('ready','sealed')
        ORDER BY s.imported_at DESC, s.id DESC
        LIMIT 1
    """, (project_name,))
    if not scan:
        raise RuntimeError(
            "no ready/sealed Scan found for release gate project {}".format(
                project_name
            )
        )
    scan_id = int(scan.get("id") or 0)
    repositories = fetchall(connection, """
        SELECT repository_name, commit_sha, identity_verified,
               identity_provenance
        FROM coverage_scan_repositories
        WHERE scan_id=?
        ORDER BY repository_name, id
    """, (scan_id,))
    reports = fetchall(connection, """
        SELECT id, report_id, report_mode
        FROM coverage_reports
        WHERE scan_id=?
        ORDER BY id
    """, (scan_id,))
    report_modes = sorted(set(
        _canonical_mode(row.get("report_mode"))
        for row in reports
        if _canonical_mode(row.get("report_mode"))
    ))
    report_mode_conflict = len(report_modes) > 1
    report_mode = report_modes[0] if len(report_modes) == 1 else "UNKNOWN"
    payload = {
        "project_name": project_name,
        "scan_id": scan_id,
        "scan_type": scan.get("scan_type") or "",
        "scan_status": scan.get("status") or "",
        "legacy_migrated": scan.get("legacy_migrated"),
        "info_file_name": scan.get("info_file_name") or "",
        "info_sha256": scan.get("info_sha256") or "",
        "repository_count": len(repositories),
        "repository_identity_complete": _repository_identity_complete(repositories),
        "report_count": len(reports),
        "report_modes": report_modes,
        "report_mode": report_mode,
        "report_mode_conflict": report_mode_conflict,
        "release_mode": release_mode,
        "claims_vnext_code_detail": bool(claims_vnext_code_detail),
    }
    return payload


def classify_current_release_gate_mode(connection, project_name, release_mode,
                                       claims_vnext_code_detail):
    inputs = collect_release_gate_inputs(
        connection, project_name, release_mode, claims_vnext_code_detail
    )
    result = classify_release_gate_mode(inputs)
    return inputs, result
