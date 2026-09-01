"""Immutable served-root publication primitives.

The application release identity describes the source checkout.  This module
describes the *published* artifact made available to a browser: one immutable
session directory contains reports, shared assets, registry metadata and the
hash manifest used to validate the actual Served Root.  ``CURRENT`` is the
only mutable pointer and is replaced atomically after validation.

The implementation intentionally uses Python 3.6-compatible standard-library
APIs so it can also be used by the older release rehearsal tooling.
"""

from __future__ import print_function

import hashlib
import html
import json
import os
import re
import shutil
import tempfile
import time

from app.release_identity import is_valid_commit_sha
from app.candidate_artifact import (
    CANDIDATE_ARTIFACT_MANIFEST_NAME, CandidateArtifactManifest,
)
from app.reports.identity import (
    LEGACY_STATIC, SUPPORTED_SIDECAR_SCHEMA_VERSIONS, VNEXT_ARTIFACT_READY,
    validate_report_id, validate_report_mode,
)
from app.time_utils import utc_iso


RELEASE_MANIFEST_SCHEMA_VERSION = 1
REPORT_MANIFEST_SCHEMA_VERSION = 1
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_META_RE = re.compile(r"<meta\b([^>]*)>", re.IGNORECASE)
_ATTR_RE = re.compile(
    r"([A-Za-z_:][A-Za-z0-9_.:-]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
    re.IGNORECASE,
)
_IDENTITY_META = {
    "coverage-report-mode": "report_mode",
    "coverage-report-id": "report_id",
    "coverage-scan-id": "scan_id",
    "coverage-repository-name": "repository_name",
    "coverage-file-path": "file_path",
    "coverage-asset-identity": "asset_identity",
    "coverage-sidecar-schema": "sidecar_schema",
    "coverage-api-contract-version": "api_contract_version",
}


def _real(path):
    return os.path.realpath(os.path.abspath(str(path)))


def _inside(root, path):
    try:
        return os.path.commonpath((_real(root), _real(path))) == _real(root)
    except ValueError:
        return False


def _validate_session_id(session_id):
    value = str(session_id or "").strip()
    if not _SESSION_RE.fullmatch(value):
        raise ValueError("invalid release-validation session id")
    return value


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_write(path, value):
    directory = os.path.dirname(_real(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    temporary = str(path) + ".tmp-{}-{}".format(
        os.getpid(), int(time.time() * 1000000)
    )
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        try:
            os.fsync(stream.fileno())
        except OSError:
            pass
    os.replace(temporary, path)


def _assert_no_symlinks(root, message):
    """Reject links anywhere below an artifact boundary.

    ``os.walk(..., followlinks=False)`` does not descend into a symlinked
    directory, so merely checking the returned filenames leaves a nested link
    invisible.  Inspect both ``dirnames`` and ``filenames`` before any copy or
    inventory operation can follow it.
    """
    root = _real(root)
    if not os.path.isdir(root):
        return
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        for name in sorted(dirnames + filenames):
            path = os.path.join(directory, name)
            if os.path.islink(path):
                raise ValueError("{}: {}".format(message, path))


def _walk_regular_files(root):
    root = _real(root)
    if not os.path.isdir(root):
        return []
    _assert_no_symlinks(root, "published artifact may not contain symlinks")
    result = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(dirnames)
        filenames = sorted(filenames)
        for name in filenames:
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                result.append(path)
    return result


def _copy_tree_contents(source_root, target_root):
    source_root = _real(source_root)
    target_root = _real(target_root)
    if not os.path.isdir(source_root):
        raise ValueError("release source root is not a directory")
    _assert_no_symlinks(source_root, "release source may not contain symlinks")
    if not os.path.isdir(target_root):
        os.makedirs(target_root)
    for name in sorted(os.listdir(source_root)):
        source = os.path.join(source_root, name)
        target = os.path.join(target_root, name)
        if os.path.isdir(source):
            shutil.copytree(source, target, symlinks=False)
        elif os.path.isfile(source):
            shutil.copy2(source, target)
        else:
            raise ValueError("unsupported release source entry: {}".format(source))


def _html_metadata(path):
    with open(path, "rb") as stream:
        raw = stream.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("report HTML exceeds the release manifest limit: {}".format(path))
    text = raw.decode("utf-8", errors="replace")
    values = {}
    for tag in _META_RE.findall(text):
        attributes = {}
        for match in _ATTR_RE.finditer(tag):
            attributes[match.group(1).lower()] = next(
                value for value in match.groups()[1:] if value is not None
            )
        key = _IDENTITY_META.get(str(attributes.get("name") or "").lower())
        if key:
            values[key] = str(attributes.get("content") or "")
    return values


def _ensure_explicit_report_modes(release_root):
    """Make Legacy mode explicit in the immutable candidate copy.

    Historical HTML may predate the report-mode metadata.  It is safe to
    annotate such a copy as Legacy, but a registry that already claims a
    VNext artifact must fail closed when its HTML omits the mode: otherwise
    the manifest would say VNext while the browser's safe default stays
    offline in Legacy mode.
    """
    reports_root = os.path.join(_real(release_root), "reports")
    for path in _walk_regular_files(reports_root):
        if not path.lower().endswith((".html", ".htm")):
            continue
        metadata = _html_metadata(path)
        report_id = str(metadata.get("report_id") or "").strip()
        registry = _registry_metadata(release_root, report_id) if report_id else {}
        declared_mode = str(metadata.get("report_mode") or "").strip()
        registry_mode = str(registry.get("report_mode") or "").strip()
        if declared_mode:
            declared = validate_report_mode(declared_mode)
            if registry_mode and validate_report_mode(registry_mode) != declared:
                raise ValueError("registry report_mode does not match HTML: {}".format(path))
            continue
        if registry_mode and validate_report_mode(registry_mode) == VNEXT_ARTIFACT_READY:
            raise ValueError("VNext report HTML lacks explicit report mode: {}".format(path))

        with open(path, "r", encoding="utf-8") as stream:
            text = stream.read()
        tag_pattern = re.compile(
            r"<meta\b(?=[^>]*\bname\s*=\s*[\"']coverage-report-mode[\"'])[^>]*>",
            re.IGNORECASE,
        )
        tag_match = tag_pattern.search(text)
        if tag_match:
            tag = tag_match.group(0)
            replacement, count = re.subn(
                r"(\bcontent\s*=\s*)([\"'])[^\"']*\2",
                r'\1"{}"'.format(LEGACY_STATIC),
                tag,
                count=1,
                flags=re.IGNORECASE,
            )
            if count != 1:
                raise ValueError("report HTML has an invalid report mode tag: {}".format(path))
            text = text[:tag_match.start()] + replacement + text[tag_match.end():]
        else:
            head = re.search(r"<head\b[^>]*>", text, re.IGNORECASE)
            if not head:
                raise ValueError("report HTML lacks <head> for explicit report mode: {}".format(path))
            insertion = '\n<meta name="coverage-report-mode" content="{}">'.format(
                LEGACY_STATIC
            )
            text = text[:head.end()] + insertion + text[head.end():]
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(text)


def _safe_relative(root, path):
    root = _real(root)
    path = _real(path)
    if not _inside(root, path):
        raise ValueError("artifact path escapes release root: {}".format(path))
    return os.path.relpath(path, root).replace(os.sep, "/")


def _registry_metadata(release_root, report_id):
    registry_path = os.path.join(release_root, "registry", validate_report_id(report_id) + ".json")
    if not os.path.isfile(registry_path):
        return {}
    with open(registry_path, "r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("report registry entry must be an object: {}".format(registry_path))
    return value


def _validate_published_sidecar(sidecar_root, report_id, expected_schema, report_path):
    """Validate the physical Sidecar identity used by a published report.

    Publication is the last point at which the whole immutable artifact is
    available.  Merely finding a non-empty ``.source_cache`` is insufficient:
    a sibling report, a stale schema, or an arbitrary file must not make a
    VNext report appear artifact-ready.
    """
    if expected_schema not in SUPPORTED_SIDECAR_SCHEMA_VERSIONS:
        raise ValueError("VNext report has unsupported Sidecar schema: {}".format(report_path))
    recognized = 0
    for name in sorted(os.listdir(sidecar_root)):
        item = os.path.join(sidecar_root, name)
        if os.path.islink(item):
            raise ValueError("VNext Sidecar may not contain symlinks: {}".format(item))
        if os.path.isdir(item):
            meta_path = os.path.join(item, "meta.json")
            if not os.path.isfile(meta_path) or os.path.islink(meta_path):
                raise ValueError("VNext Sidecar metadata is missing: {}".format(item))
            with open(meta_path, "r", encoding="utf-8") as stream:
                metadata = json.load(stream)
            if not isinstance(metadata, dict):
                raise ValueError("VNext Sidecar metadata is not an object: {}".format(meta_path))
            actual_schema = int(metadata.get("schema_version") or 0)
            if actual_schema != expected_schema:
                raise ValueError("Sidecar schema identity mismatch: {}".format(meta_path))
            if str(metadata.get("report_id") or "") != report_id:
                raise ValueError("Sidecar report identity mismatch: {}".format(meta_path))
            recognized += 1
        elif name == "meta.json":
            # Keep explicitly identified v1 fixtures/artifacts that store one
            # metadata file directly under the report cache root.  The normal
            # v2 writer nests this file under a per-file key directory.
            with open(item, "r", encoding="utf-8") as stream:
                metadata = json.load(stream)
            if not isinstance(metadata, dict):
                raise ValueError("VNext Sidecar metadata is not an object: {}".format(item))
            actual_schema = int(metadata.get("schema_version") or 0)
            if actual_schema != expected_schema:
                raise ValueError("Sidecar schema identity mismatch: {}".format(item))
            if str(metadata.get("report_id") or "") != report_id:
                raise ValueError("Sidecar report identity mismatch: {}".format(item))
            recognized += 1
        elif name.endswith(".source.json"):
            if expected_schema != 1:
                raise ValueError("legacy Sidecar does not match VNext schema: {}".format(item))
            with open(item, "r", encoding="utf-8") as stream:
                metadata = json.load(stream)
            if not isinstance(metadata, dict):
                raise ValueError("legacy Sidecar is not an object: {}".format(item))
            if str(metadata.get("report_id") or "") != report_id:
                raise ValueError("Sidecar report identity mismatch: {}".format(item))
            recognized += 1
    if not recognized:
        raise ValueError("VNext report has no recognized Sidecar metadata: {}".format(report_path))


def _report_entries(release_root, reports_root):
    entries = []
    for path in _walk_regular_files(reports_root):
        if not path.lower().endswith((".html", ".htm")):
            continue
        metadata = _html_metadata(path)
        registry = {}
        report_id = str(metadata.get("report_id") or "").strip()
        if report_id:
            registry = _registry_metadata(release_root, report_id)
        declared_mode = str(metadata.get("report_mode") or "").strip()
        if not declared_mode:
            raise ValueError("report HTML lacks explicit report mode: {}".format(path))
        mode = validate_report_mode(declared_mode)
        registry_mode = str(registry.get("report_mode") or "").strip()
        if registry_mode and validate_report_mode(registry_mode) != mode:
            raise ValueError("registry report_mode does not match HTML: {}".format(path))
        scan_id = str(metadata.get("scan_id") or registry.get("scan_id") or "").strip()
        repository_name = str(
            metadata.get("repository_name") or registry.get("repository_name") or ""
        )
        file_path = str(metadata.get("file_path") or "")
        sidecar_schema = int(
            metadata.get("sidecar_schema") or registry.get("sidecar_schema") or 0
        )
        asset_identity = str(
            metadata.get("asset_identity") or registry.get("asset_identity") or ""
        )
        registry_root = str(registry.get("report_root") or "")
        sidecar_relative = ""
        if report_id:
            candidate = os.path.join(reports_root, ".source_cache", report_id)
            if os.path.isdir(candidate) and not os.path.islink(candidate):
                sidecar_relative = _safe_relative(release_root, candidate)
        if mode == VNEXT_ARTIFACT_READY:
            if not str(metadata.get("report_id") or "") or not report_id or \
                    not scan_id.isdigit() or int(scan_id) <= 0:
                raise ValueError("VNext report HTML lacks exact report_id/scan_id: {}".format(path))
            if str(metadata.get("scan_id") or "") != scan_id:
                raise ValueError("VNext report HTML lacks exact scan identity: {}".format(path))
            if not file_path or not str(metadata.get("file_path") or ""):
                raise ValueError("VNext report HTML lacks exact file_path: {}".format(path))
            if not repository_name or not str(metadata.get("repository_name") or ""):
                raise ValueError("VNext report HTML lacks exact repository identity: {}".format(path))
            if not asset_identity or not str(metadata.get("asset_identity") or ""):
                raise ValueError("VNext report HTML lacks asset_identity: {}".format(path))
            if int(metadata.get("sidecar_schema") or 0) != sidecar_schema:
                raise ValueError("VNext report HTML lacks exact Sidecar schema: {}".format(path))
            sidecar_files = _walk_regular_files(candidate) if sidecar_relative else []
            if sidecar_schema <= 0 or not sidecar_relative or not sidecar_files:
                raise ValueError("VNext report HTML lacks a supported Sidecar: {}".format(path))
            _validate_published_sidecar(
                candidate, report_id, sidecar_schema, path
            )
            if not registry:
                raise ValueError("VNext report HTML lacks an exact registry entry: {}".format(path))
            if str(registry.get("report_id") or "") != report_id:
                raise ValueError("registry report_id does not match HTML: {}".format(path))
            if validate_report_mode(registry.get("report_mode"), default=LEGACY_STATIC) != mode:
                raise ValueError("registry report_mode does not match HTML: {}".format(path))
            if registry.get("scan_id") is None or str(registry.get("scan_id")) != scan_id:
                raise ValueError("registry scan_id does not match HTML: {}".format(path))
            if str(registry.get("asset_identity") or "") != asset_identity:
                raise ValueError("registry asset_identity does not match HTML: {}".format(path))
            if int(registry.get("sidecar_schema") or 0) != sidecar_schema:
                raise ValueError("registry sidecar schema does not match HTML: {}".format(path))
            if not registry_root:
                raise ValueError("VNext registry lacks an exact report_root: {}".format(path))
            root_value = registry_root.replace("\\", "/").strip()
            if os.path.isabs(registry_root):
                root_matches = _real(registry_root) == _real(reports_root)
            else:
                root_matches = _real(os.path.join(release_root, root_value)) == _real(reports_root)
            if not root_matches:
                raise ValueError("registry report_root does not match Served Root: {}".format(path))
            registered_repositories = registry.get("repository_names") or []
            if registered_repositories and repository_name not in set(
                    str(item or "") for item in registered_repositories):
                raise ValueError("registry repository identity does not match HTML: {}".format(path))
        entries.append({
            "path": _safe_relative(release_root, path),
            "sha256": _sha256(path),
            "size": int(os.path.getsize(path)),
            "report_mode": mode,
            "report_id": report_id,
            "scan_id": int(scan_id) if scan_id.isdigit() else None,
            "repository_name": repository_name,
            "file_path": file_path,
            "report_root": registry_root or "reports",
            "asset_identity": asset_identity,
            "sidecar_schema": sidecar_schema,
            "sidecar_path": sidecar_relative,
            "registry_path": (
                _safe_relative(
                    release_root,
                    os.path.join(release_root, "registry", report_id + ".json")
                ) if report_id and os.path.isfile(
                    os.path.join(release_root, "registry", report_id + ".json")
                ) else ""
            ),
        })
    return sorted(entries, key=lambda item: item["path"])


def _file_manifest(root, directories):
    entries = []
    for directory in directories:
        path = os.path.join(root, directory)
        for file_path in _walk_regular_files(path):
            entries.append({
                "path": _safe_relative(root, file_path),
                "size": int(os.path.getsize(file_path)),
                "sha256": _sha256(file_path),
            })
    return sorted(entries, key=lambda item: item["path"])


def _inventory_hash(entries):
    return hashlib.sha256(
        json.dumps(
            list(entries), ensure_ascii=False, sort_keys=True,
            separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def build_release_manifest(release_root, release_identity, session_id,
                           api_contract_version="", candidate_sha="",
                           published_root=None, candidate_artifact_manifest=None):
    """Build and validate the manifest for an already prepared release root."""
    release_root = _real(release_root)
    published_root = _real(published_root or release_root)
    _assert_no_symlinks(release_root, "published artifact may not contain symlinks")
    session_id = _validate_session_id(session_id)
    identity = dict(release_identity or {})
    commit_sha = str(candidate_sha or identity.get("commit_sha") or "").strip()
    if not is_valid_commit_sha(commit_sha):
        raise ValueError("release manifest requires an exact commit SHA")
    reports_root = os.path.join(release_root, "reports")
    assets_root = os.path.join(release_root, "assets")
    registry_root = os.path.join(release_root, "registry")
    if not os.path.isdir(reports_root):
        raise ValueError("immutable release is missing reports/")
    if not os.path.isdir(assets_root):
        raise ValueError("immutable release is missing assets/")
    if not os.path.isdir(registry_root):
        raise ValueError("immutable release is missing registry/")
    reports = _report_entries(release_root, reports_root)
    shared_assets = _file_manifest(release_root, ("assets",))
    all_files = _file_manifest(release_root, ("reports", "assets", "registry"))
    manifest = {
        "schema_version": RELEASE_MANIFEST_SCHEMA_VERSION,
        "release_validation_session_id": session_id,
        "commit_sha": commit_sha,
        "build_id": str(identity.get("build_id") or ""),
        "release_identity": identity,
        "api_contract_version": str(api_contract_version or ""),
        "release_root": _safe_relative(
            os.path.dirname(published_root), published_root
        ),
        "served_root": {
            "relative": "reports",
            "path": os.path.join(published_root, "reports"),
            "sha256": _inventory_hash(
                [item for item in all_files if item["path"].startswith("reports/")]
            ),
        },
        "reports": reports,
        "report_ids": sorted(set(item["report_id"] for item in reports if item["report_id"])),
        "scan_ids": sorted(set(item["scan_id"] for item in reports if item["scan_id"] is not None)),
        "report_modes": sorted(set(item["report_mode"] for item in reports)),
        "shared_assets": shared_assets,
        "registry_files": [item for item in all_files if item["path"].startswith("registry/")],
        "files": all_files,
        "generated_at": utc_iso(),
    }
    if candidate_artifact_manifest:
        artifact_path = os.path.join(
            release_root,
            str(candidate_artifact_manifest.get("manifest_path") or
                CANDIDATE_ARTIFACT_MANIFEST_NAME).replace("/", os.sep),
        )
        if not os.path.isfile(artifact_path):
            raise ValueError("candidate artifact manifest was not copied into release")
        manifest["candidate_artifact_manifest"] = {
            "manifest_path": _safe_relative(release_root, artifact_path),
            "manifest_sha256": _sha256(artifact_path),
            "artifact_manifest_version": candidate_artifact_manifest.get(
                "artifact_manifest_version"
            ),
            "commit_sha": candidate_artifact_manifest.get("commit_sha"),
            "build_id": candidate_artifact_manifest.get("build_id"),
            "artifact_sha256": candidate_artifact_manifest.get("artifact_sha256"),
            "reports_sha256": candidate_artifact_manifest.get("reports_sha256"),
            "assets_sha256": candidate_artifact_manifest.get("assets_sha256"),
            "registry_sha256": candidate_artifact_manifest.get("registry_sha256"),
        }
    return manifest


def _load_json(path):
    with open(path, "r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("release manifest must be a JSON object")
    return value


def validate_release_manifest(release_root, manifest=None, expected_session_id=""):
    """Verify hashes, actual Served Root and report mode identity."""
    release_root = _real(release_root)
    manifest = manifest if manifest is not None else _load_json(
        os.path.join(release_root, "release_manifest.json")
    )
    violations = []
    session_id = str(manifest.get("release_validation_session_id") or "")
    try:
        _validate_session_id(session_id)
    except ValueError as exc:
        violations.append(str(exc))
    if expected_session_id and session_id != str(expected_session_id):
        violations.append("release-validation session mismatch")
    if int(manifest.get("schema_version") or 0) != RELEASE_MANIFEST_SCHEMA_VERSION:
        violations.append("unsupported release manifest schema")
    actual_commit = str(manifest.get("commit_sha") or "")
    if not is_valid_commit_sha(actual_commit):
        violations.append("release manifest commit_sha is not exact")
    served = manifest.get("served_root") or {}
    expected_served = _real(os.path.join(release_root, str(served.get("relative") or "")))
    if not os.path.isdir(expected_served):
        violations.append("served root is missing")
    if _real(served.get("path") or "") != expected_served:
        violations.append("manifest Served Root does not match actual release root")
    try:
        _assert_no_symlinks(
            release_root, "published artifact may not contain symlinks"
        )
        observed_files = {
            item["path"]: item for item in _file_manifest(
                release_root, ("reports", "assets", "registry")
            )
        }
    except (OSError, ValueError) as exc:
        observed_files = {}
        violations.append(str(exc))
    declared_files = {
        item.get("path"): item for item in manifest.get("files") or []
        if isinstance(item, dict) and item.get("path")
    }
    if set(observed_files) != set(declared_files):
        violations.append("release file inventory changed")
    for path, declared in declared_files.items():
        observed = observed_files.get(path)
        if not observed or observed.get("sha256") != declared.get("sha256") or \
                int(observed.get("size") or 0) != int(declared.get("size") or 0):
            violations.append("release file hash changed: {}".format(path))
    observed_reports = []
    if os.path.isdir(os.path.join(release_root, "reports")):
        try:
            observed_reports = _report_entries(
                release_root, os.path.join(release_root, "reports")
            )
        except (OSError, ValueError, TypeError) as exc:
            violations.append(str(exc))
    declared_reports = manifest.get("reports") or []
    if observed_reports != declared_reports:
        violations.append("report entry identity or hash changed")
    declared_served_hash = str(served.get("sha256") or "")
    observed_served_hash = _inventory_hash(
        [item for item in observed_files.values() if item["path"].startswith("reports/")]
    ) if observed_files else ""
    if declared_served_hash != observed_served_hash:
        violations.append("actual Served Root hash changed")
    candidate_artifact = manifest.get("candidate_artifact_manifest") or {}
    if candidate_artifact:
        relative = str(candidate_artifact.get("manifest_path") or "")
        candidate_path = _real(os.path.join(release_root, relative))
        if not relative or not _inside(release_root, candidate_path) or \
                not os.path.isfile(candidate_path):
            violations.append("candidate artifact manifest is missing from release")
        elif str(candidate_artifact.get("manifest_sha256") or "") != _sha256(candidate_path):
            violations.append("candidate artifact manifest hash changed")
    return {
        "status": "PASSED" if not violations else "FAILED",
        "release_validation_session_id": session_id,
        "commit_sha": actual_commit,
        "served_root": expected_served,
        "violations": violations,
    }


class ImmutableReleasePublisher(object):
    """Prepare immutable releases and atomically switch/rollback CURRENT."""

    def __init__(self, publish_root):
        self.publish_root = _real(publish_root)
        self.releases_root = os.path.join(self.publish_root, "releases")
        self.current_path = os.path.join(self.publish_root, "CURRENT")
        if not os.path.isdir(self.releases_root):
            os.makedirs(self.releases_root)

    def release_path(self, session_id):
        return os.path.join(self.releases_root, _validate_session_id(session_id))

    def prepare(self, source_root, release_identity, session_id,
                api_contract_version="", candidate_sha="",
                candidate_artifact_manifest=""):
        session_id = _validate_session_id(session_id)
        final_root = self.release_path(session_id)
        if os.path.lexists(final_root):
            raise ValueError("immutable release already exists: {}".format(session_id))
        candidate_manifest_path = candidate_artifact_manifest or os.path.join(
            _real(source_root), CANDIDATE_ARTIFACT_MANIFEST_NAME
        )
        if not os.path.isabs(str(candidate_manifest_path)):
            candidate_manifest_path = os.path.join(
                _real(source_root), str(candidate_manifest_path)
            )
        verified_candidate_manifest = CandidateArtifactManifest.verify(
            source_root, release_identity, candidate_sha=candidate_sha,
            manifest_path=candidate_manifest_path,
        )
        staging = tempfile.mkdtemp(prefix=".release-{}-".format(session_id),
                                   dir=self.releases_root)
        try:
            _copy_tree_contents(source_root, staging)
            for directory in ("reports", "assets", "registry"):
                path = os.path.join(staging, directory)
                if not os.path.isdir(path):
                    raise ValueError("release source is missing {}/".format(directory))
            _ensure_explicit_report_modes(staging)
            # Build against the final path so the persisted Served Root is the
            # path Nginx will actually resolve after the atomic rename.
            manifest = build_release_manifest(
                staging, release_identity, session_id,
                api_contract_version=api_contract_version,
                candidate_sha=candidate_sha,
                published_root=final_root,
                candidate_artifact_manifest=verified_candidate_manifest,
            )
            report_manifest = {
                "schema_version": REPORT_MANIFEST_SCHEMA_VERSION,
                "release_validation_session_id": session_id,
                "reports": manifest["reports"],
            }
            _json_write(os.path.join(staging, "report_manifest.json"), report_manifest)
            _json_write(os.path.join(staging, "release_manifest.json"), manifest)
            # The manifests themselves are intentionally outside the hashed
            # runtime inventory; they describe the immutable payload.
            os.replace(staging, final_root)
            staging = None
            checked = validate_release_manifest(final_root, manifest, session_id)
            if checked["status"] != "PASSED":
                raise RuntimeError("prepared release failed validation: {}".format(
                    "; ".join(checked["violations"])
                ))
            return manifest
        finally:
            if staging and os.path.isdir(staging):
                shutil.rmtree(staging)

    def current_session_id(self):
        if not os.path.islink(self.current_path):
            return ""
        target = _real(os.path.join(self.publish_root, os.readlink(self.current_path)))
        if not _inside(self.releases_root, target) or not os.path.isdir(target):
            return ""
        return os.path.basename(target)

    def switch_current(self, session_id):
        session_id = _validate_session_id(session_id)
        target = self.release_path(session_id)
        checked = validate_release_manifest(target, expected_session_id=session_id)
        if checked["status"] != "PASSED":
            raise RuntimeError("cannot publish invalid release: {}".format(
                "; ".join(checked["violations"])
            ))
        temporary = os.path.join(
            self.publish_root, ".CURRENT-{}-{}".format(
                os.getpid(), int(time.time() * 1000000)
            )
        )
        os.symlink(os.path.relpath(target, self.publish_root), temporary)
        try:
            os.replace(temporary, self.current_path)
        finally:
            try:
                os.remove(temporary)
            except OSError:
                pass
        return {
            "status": "PASSED",
            "release_validation_session_id": session_id,
            "served_root": checked["served_root"],
        }

    def validate_current(self):
        session_id = self.current_session_id()
        if not session_id:
            return {"status": "FAILED", "violations": ["CURRENT is not a valid release pointer"]}
        return validate_release_manifest(
            self.release_path(session_id), expected_session_id=session_id
        )

    def rollback(self, session_id):
        # Rollback uses the same validation and atomic pointer operation as a
        # forward publish. It never rewrites the database or report facts.
        return self.switch_current(session_id)
