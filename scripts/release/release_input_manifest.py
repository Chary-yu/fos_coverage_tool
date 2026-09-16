"""Canonical, offline release-input manifest.

The release-input manifest is the immutable hand-off between a clean local
checkout and the preparation-only production operator.  It deliberately has
no GitHub-specific fields: source, tooling, baseline, host, and session
identity are all explicit values carried by the operator.

This module uses only Python 3.6-compatible standard-library features because
the same contract is consumed by the production-side runtime.
"""

from __future__ import print_function

import hashlib
import json
import os
import re


RELEASE_INPUT_MANIFEST_SCHEMA_VERSION = 1
RELEASE_INPUT_MANIFEST_FILENAME = "release_input_manifest.json"
RELEASE_INPUT_MANIFEST_DIGEST_FILENAME = "release_input_manifest.sha256"
RELEASE_TRUST_MODE_OFFLINE_OPERATOR = "offline_operator"

_SHA40 = re.compile(r"^[0-9a-fA-F]{40}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

REQUIRED_RELEASE_INPUT_FIELDS = (
    "schema_version",
    "repository",
    "source_commit_sha",
    "source_tree_sha",
    "source_bundle_sha256",
    "tooling_commit_sha",
    "tooling_tree_sha",
    "tooling_bundle_sha256",
    "production_host",
    "baseline_commit_sha",
    "baseline_attempt_id",
    "baseline_tree_sha256",
    "baseline_manifest_sha256",
    "baseline_adoption_identity_sha256",
    "project_name",
    "candidate_parent",
    "release_trust_mode",
)

_SHA40_FIELDS = (
    "source_commit_sha", "source_tree_sha", "tooling_commit_sha",
    "tooling_tree_sha", "baseline_commit_sha",
)
_SHA256_FIELDS = (
    "source_bundle_sha256", "tooling_bundle_sha256",
    "baseline_tree_sha256", "baseline_manifest_sha256",
    "baseline_adoption_identity_sha256",
)
_PATH_FIELDS = (
    "candidate_parent", "baseline_root", "baseline_manifest_path",
    "baseline_adoption_identity_path", "flat_served_root",
    "flat_release_identity_path", "production_publish_root",
    "application_root", "served_root", "publish_root",
)
_OPTIONAL_SHA256_FIELDS = ("operator_sha256", "release_manifest_sha256")
_OPTIONAL_ID_FIELDS = ("baseline_attempt_id", "validation_session_id")
_ALLOWED_BUILD_MODES = (
    "production", "flat_production", "validation_fixture",
)


def canonical_json_bytes(value):
    """Serialize a manifest deterministically for hashing and embedding."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _exact(value, pattern, label):
    normalized = str(value or "").strip().lower()
    if not pattern.fullmatch(normalized) or normalized == "0" * len(normalized):
        raise ValueError("{} must be a concrete exact SHA".format(label))
    return normalized


def _required_text(value, label):
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("{} is required".format(label))
    if "\x00" in normalized:
        raise ValueError("{} contains a NUL byte".format(label))
    return normalized


def _safe_path(value, label, required=False):
    normalized = _required_text(value, label) if required or value else ""
    if not normalized:
        return normalized
    if not os.path.isabs(normalized):
        raise ValueError("{} must be absolute".format(label))
    components = normalized.replace("\\", "/").split("/")
    if ".." in components:
        raise ValueError("{} contains path traversal".format(label))
    return normalized


def _copy_normalized(manifest):
    normalized = dict(manifest)
    for field in _SHA40_FIELDS:
        normalized[field] = _exact(normalized.get(field), _SHA40, field)
    for field in _SHA256_FIELDS:
        normalized[field] = _exact(normalized.get(field), _SHA256, field)
    for field in _PATH_FIELDS:
        normalized[field] = _safe_path(
            normalized.get(field), field, required=(field == "candidate_parent")
        )
    for field in _OPTIONAL_SHA256_FIELDS:
        if normalized.get(field) not in (None, ""):
            normalized[field] = _exact(normalized[field], _SHA256, field)
    for field in _OPTIONAL_ID_FIELDS:
        if normalized.get(field) not in (None, ""):
            normalized[field] = _required_text(normalized[field], field)
            if not _SAFE_ID.fullmatch(normalized[field]):
                raise ValueError("{} contains unsafe characters".format(field))
    return normalized


def validate_release_input_manifest(manifest):
    """Validate and normalize one release-input manifest.

    The function returns a new dictionary with normalized SHA casing.  The
    optional ``production_publishable`` field is explicit when present; a
    validation fixture may only carry ``False`` and can never carry ``True``.
    """
    if not isinstance(manifest, dict):
        raise ValueError("release input manifest must be a JSON object")
    missing = [key for key in REQUIRED_RELEASE_INPUT_FIELDS if key not in manifest]
    if missing:
        raise ValueError(
            "release input manifest is missing {}".format(",".join(missing))
        )
    normalized = dict(manifest)
    schema_version = normalized.get("schema_version")
    if isinstance(schema_version, bool) or \
            schema_version != RELEASE_INPUT_MANIFEST_SCHEMA_VERSION:
        raise ValueError("schema_version must be 1")
    repository = _required_text(normalized.get("repository"), "repository")
    if not _SAFE_REPOSITORY.fullmatch(repository):
        raise ValueError("repository is not a canonical owner/name")
    normalized["repository"] = repository
    normalized["production_host"] = _required_text(
        normalized.get("production_host"), "production_host"
    )
    if not _SAFE_ID.fullmatch(normalized["production_host"]):
        raise ValueError("production_host contains unsafe characters")
    normalized["baseline_attempt_id"] = _required_text(
        normalized.get("baseline_attempt_id"), "baseline_attempt_id"
    )
    if not _SAFE_ID.fullmatch(normalized["baseline_attempt_id"]):
        raise ValueError("baseline_attempt_id contains unsafe characters")
    normalized["project_name"] = _required_text(
        normalized.get("project_name"), "project_name"
    )
    if not _SAFE_ID.fullmatch(normalized["project_name"]):
        raise ValueError("project_name contains unsafe characters")
    normalized["release_trust_mode"] = _required_text(
        normalized.get("release_trust_mode"), "release_trust_mode"
    )
    if normalized["release_trust_mode"] != RELEASE_TRUST_MODE_OFFLINE_OPERATOR:
        raise ValueError(
            "release_trust_mode must be offline_operator; protected_builder "
            "is legacy verify-only"
        )
    normalized = _copy_normalized(normalized)
    build_mode = normalized.get("candidate_build_mode", "production")
    if build_mode not in _ALLOWED_BUILD_MODES:
        raise ValueError("candidate_build_mode is unsupported")
    normalized["candidate_build_mode"] = build_mode
    if "production_publishable" in normalized and \
            not isinstance(normalized["production_publishable"], bool):
        raise ValueError("production_publishable must be boolean")
    if normalized.get("production_publishable") is True:
        raise ValueError(
            "production_publishable must be false until VFOS production validation"
        )
    if build_mode == "validation_fixture" and \
            normalized.get("production_publishable", False) is not False:
        raise ValueError("validation fixture production_publishable must be false")
    if normalized.get("candidate_parent") == "/":
        raise ValueError("candidate_parent is too broad")
    return normalized


def build_release_input_manifest(values=None, **overrides):
    """Build a validated manifest from a mapping and optional overrides."""
    manifest = dict(values or {})
    manifest.update(overrides)
    manifest.setdefault("schema_version", RELEASE_INPUT_MANIFEST_SCHEMA_VERSION)
    return validate_release_input_manifest(manifest)


def release_input_manifest_sha256(manifest):
    """Return the digest of the canonical validated manifest bytes."""
    normalized = validate_release_input_manifest(manifest)
    return _digest_bytes(canonical_json_bytes(normalized))


def write_release_input_manifest(path, manifest, sidecar_path=None):
    """Write canonical JSON and its detached SHA256 sidecar atomically."""
    normalized = validate_release_input_manifest(manifest)
    digest = _digest_bytes(canonical_json_bytes(normalized))
    path = os.path.abspath(str(path))
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    temporary = "{}.tmp-{}".format(path, os.getpid())
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(normalized, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        try:
            os.fsync(stream.fileno())
        except OSError:
            pass
    os.replace(temporary, path)
    if sidecar_path is None:
        sidecar_path = os.path.join(parent, RELEASE_INPUT_MANIFEST_DIGEST_FILENAME)
    sidecar_path = os.path.abspath(str(sidecar_path))
    sidecar_parent = os.path.dirname(sidecar_path)
    if sidecar_parent and not os.path.isdir(sidecar_parent):
        os.makedirs(sidecar_parent)
    sidecar_temporary = "{}.tmp-{}".format(sidecar_path, os.getpid())
    with open(sidecar_temporary, "w", encoding="ascii") as stream:
        stream.write("{}  {}\n".format(digest, os.path.basename(path)))
        stream.flush()
        try:
            os.fsync(stream.fileno())
        except OSError:
            pass
    os.replace(sidecar_temporary, sidecar_path)
    return digest


def load_release_input_manifest(path, expected_sha256=None):
    """Load, validate, and optionally verify one manifest file."""
    path = os.path.abspath(str(path))
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError("release input manifest is missing or linked: {}".format(path))
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("release input manifest is unreadable: {}".format(exc))
    normalized = validate_release_input_manifest(value)
    observed = _digest_bytes(canonical_json_bytes(normalized))
    if expected_sha256 not in (None, ""):
        expected = _exact(expected_sha256, _SHA256, "release input manifest SHA256")
        if observed != expected:
            raise ValueError("release input manifest SHA256 mismatch")
    return normalized


def verify_release_input_manifest(path, expected_sha256):
    """Compatibility-friendly explicit verification entry point."""
    return load_release_input_manifest(path, expected_sha256=expected_sha256)


def manifest_binding(manifest):
    """Return only the immutable identity fields used in release evidence."""
    normalized = validate_release_input_manifest(manifest)
    return {
        key: normalized[key]
        for key in REQUIRED_RELEASE_INPUT_FIELDS
    }
