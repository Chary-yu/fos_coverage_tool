"""Prepare a real flat legacy Served Root for explicit immutable adoption.

The historical production layout predates the immutable release contract: its
HTML and static assets live directly under one directory, and it has no
``reports/``, ``assets/`` or ``registry/`` tree.  This tool creates a separate
staging tree for that one-time adoption.  It preserves every source byte,
except for adding the minimum deterministic identity metadata to each HTML
file.  It never invents scan, repository, file, Sidecar or asset identities.

The output is intentionally suitable only as the input to the dedicated
``bootstrap_previous_release.py`` path.  Normal production Candidate builds
continue to enforce the complete production content contract.
"""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.candidate_artifact import PRODUCTION_PROJECT_NAME
from app.release_identity import is_valid_commit_sha
from app.reports.identity import LEGACY_STATIC, validate_report_id


IDENTITY_KEYS = (
    "version", "commit_sha", "build_id", "asset_hash", "schema_version",
    "asset_manifest_version", "asset_count", "asset_manifest_hash",
    "asset_manifest",
)
_IDENTITY_META_NAMES = frozenset((
    "coverage-project", "coverage-report-mode", "coverage-report-id",
    "coverage-scan-id", "coverage-repository-name", "coverage-file-path",
    "coverage-asset-identity", "coverage-sidecar-schema",
    "coverage-api-contract-version",
))
_CONTROL_NAMES = frozenset((
    "CURRENT", "candidate_artifact_manifest.json",
    "candidate_build_attestation.json", "candidate_build_receipt.json",
    "release_identity.json", "release_manifest.json", "report_manifest.json",
    "validated_publication_identity.json",
))
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_META_RE = re.compile(r"<meta\b([^>]*)>", re.IGNORECASE)
_ATTR_RE = re.compile(
    r"([A-Za-z_:][A-Za-z0-9_.:-]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
    re.IGNORECASE,
)
_HEAD_OPEN_RE = re.compile(br"<head\b[^>]*>", re.IGNORECASE)
_HEAD_CLOSE_RE = re.compile(br"</head\s*>", re.IGNORECASE)


def _real(path):
    return os.path.realpath(os.path.abspath(str(path)))


def _inside(root, path):
    try:
        return os.path.commonpath((_real(root), _real(path))) == _real(root)
    except ValueError:
        return False


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _load_json(path, description):
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("{} is unreadable: {}".format(description, exc))
    if not isinstance(value, dict):
        raise ValueError("{} must be a JSON object".format(description))
    return value


def _validate_release_identity(path, expected_commit_sha):
    identity = _load_json(path, "legacy release identity")
    missing = [key for key in IDENTITY_KEYS if identity.get(key) in (None, "")]
    if missing:
        raise ValueError(
            "legacy release identity is incomplete: {}".format(", ".join(missing))
        )
    expected_commit_sha = str(expected_commit_sha or "").strip()
    if not is_valid_commit_sha(expected_commit_sha):
        raise ValueError("expected legacy release commit SHA must be exact")
    commit_sha = str(identity.get("commit_sha") or "").strip()
    if not is_valid_commit_sha(commit_sha):
        raise ValueError("legacy release identity commit_sha is not exact")
    if commit_sha.lower() != expected_commit_sha.lower():
        raise ValueError("legacy release identity does not match expected commit SHA")
    if isinstance(identity.get("release_identity"), dict):
        raise ValueError("legacy release identity must be a direct identity JSON object")

    declared_assets = identity.get("asset_manifest")
    if not isinstance(declared_assets, list) or not declared_assets:
        raise ValueError("legacy release identity asset_manifest is invalid")
    seen = set()
    for item in declared_assets:
        if not isinstance(item, dict):
            raise ValueError("legacy release identity asset_manifest is invalid")
        relative = str(item.get("path") or "").replace("\\", "/")
        parts = relative.split("/")
        if not relative or relative.startswith("/") or ".." in parts:
            raise ValueError("legacy release identity asset path is invalid: {}".format(relative))
        if relative in seen:
            raise ValueError("legacy release identity has duplicate asset: {}".format(relative))
        seen.add(relative)
        try:
            size = int(item.get("size"))
        except (TypeError, ValueError):
            raise ValueError("legacy release identity asset size is invalid: {}".format(relative))
        if size < 0 or not _SHA256_RE.fullmatch(str(item.get("sha256") or "")):
            raise ValueError("legacy release identity asset fingerprint is invalid: {}".format(relative))
    if int(identity.get("asset_count") or 0) != len(declared_assets):
        raise ValueError("legacy release identity asset_count does not match asset_manifest")
    asset_hash = _canonical_hash(declared_assets)
    if str(identity.get("asset_hash") or "").lower() != asset_hash.lower() or \
            str(identity.get("asset_manifest_hash") or "").lower() != asset_hash.lower():
        raise ValueError("legacy release identity asset manifest hash is invalid")
    if int(identity.get("asset_manifest_version") or 0) <= 0:
        raise ValueError("legacy release identity asset_manifest_version is invalid")
    return identity


def _identity_meta_names(raw):
    text = raw.decode("utf-8", errors="replace")
    names = set()
    for tag in _META_RE.findall(text):
        attributes = {}
        for match in _ATTR_RE.finditer(tag):
            attributes[match.group(1).lower()] = next(
                value for value in match.groups()[1:] if value is not None
            )
        name = str(attributes.get("name") or "").strip().lower()
        if name in _IDENTITY_META_NAMES:
            names.add(name)
    return names


def _legacy_report_id(relative_path, source_sha256):
    payload = "{}\0{}".format(relative_path.replace("\\", "/"), source_sha256)
    report_id = "legacy_{}".format(
        hashlib.sha256(payload.encode("utf-8")).hexdigest()[:56]
    )
    return validate_report_id(report_id)


def _add_legacy_identity_meta(raw, report_id):
    existing = _identity_meta_names(raw)
    if existing:
        raise ValueError(
            "legacy HTML already contains coverage identity metadata: {}".format(
                ", ".join(sorted(existing))
            )
        )
    opening = _HEAD_OPEN_RE.search(raw)
    closing = _HEAD_CLOSE_RE.search(raw)
    if not opening or not closing or closing.start() < opening.end():
        raise ValueError("legacy HTML must contain a complete <head>: report_id={}".format(report_id))
    additions = (
        '\n<meta name="coverage-project" content="{}">\n'
        '<meta name="coverage-report-mode" content="{}">\n'
        '<meta name="coverage-report-id" content="{}">'
    ).format(PRODUCTION_PROJECT_NAME, LEGACY_STATIC, report_id).encode("ascii")
    return raw[:opening.end()] + additions + raw[opening.end():]


def _validate_flat_root(flat_root):
    requested_root = os.path.abspath(str(flat_root))
    if os.path.islink(requested_root) or not os.path.isdir(requested_root):
        raise ValueError("legacy Flat Root must be a real directory")
    flat_root = _real(requested_root)
    files = []
    html_files = []
    for name in sorted(os.listdir(flat_root)):
        path = os.path.join(flat_root, name)
        if os.path.islink(path) or not os.path.isfile(path):
            raise ValueError(
                "legacy Flat Root must contain only regular root-level files: {}".format(path)
            )
        if name in _CONTROL_NAMES:
            raise ValueError("legacy Flat Root contains release control file: {}".format(name))
        files.append((name, path))
        if name.lower().endswith((".html", ".htm")):
            html_files.append((name, path))
    if not html_files:
        raise ValueError("legacy Flat Root contains no root-level HTML reports")
    non_html_count = len(files) - len(html_files)
    if non_html_count <= 0:
        raise ValueError("legacy Flat Root contains no Served static assets")
    for name, path in html_files:
        with open(path, "rb") as stream:
            raw = stream.read()
        existing = _identity_meta_names(raw)
        if existing:
            raise ValueError(
                "legacy Flat Root HTML must not already contain coverage identity metadata: {}".format(
                    path
                )
            )
    return flat_root, files, html_files


def _write_bytes(path, value):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "wb") as stream:
        stream.write(value)


def _write_json(path, value):
    _write_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def prepare_legacy_flat_adoption(flat_root, output_root, release_identity_path,
                                 expected_commit_sha):
    """Create an isolated adoption tree from a root-level legacy release."""
    flat_root, files, html_files = _validate_flat_root(flat_root)
    output_root = os.path.abspath(str(output_root))
    if os.path.lexists(output_root):
        raise ValueError("legacy adoption output must not already exist")
    if _inside(flat_root, output_root) or _inside(output_root, flat_root):
        raise ValueError("legacy adoption output must be separate from Flat Root")
    requested_identity_path = os.path.abspath(str(release_identity_path))
    if os.path.islink(requested_identity_path) or not os.path.isfile(
            requested_identity_path):
        raise ValueError("legacy release identity file is missing or linked")
    release_identity_path = _real(requested_identity_path)
    identity = _validate_release_identity(
        release_identity_path, expected_commit_sha
    )

    parent = os.path.dirname(output_root)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    temporary = tempfile.mkdtemp(prefix=".legacy-flat-adoption-", dir=parent or None)
    reports_root = os.path.join(temporary, "reports")
    assets_root = os.path.join(temporary, "assets")
    registry_root = os.path.join(temporary, "registry")
    os.makedirs(reports_root)
    os.makedirs(assets_root)
    os.makedirs(registry_root)
    report_entries = []
    asset_names = []
    try:
        # Keep the supplied identity bytes intact.  The identity is evidence
        # for the exact historical release; adoption must not regenerate it.
        shutil.copyfile(
            release_identity_path,
            os.path.join(temporary, "release_identity.json"),
        )
        html_names = set(name for name, _ in html_files)
        for name, source_path in files:
            relative = name.replace(os.sep, "/")
            with open(source_path, "rb") as stream:
                original = stream.read()
            source_sha256 = hashlib.sha256(original).hexdigest()
            report_path = os.path.join(reports_root, name)
            if name in html_names:
                report_id = _legacy_report_id(relative, source_sha256)
                prepared = _add_legacy_identity_meta(original, report_id)
                _write_bytes(report_path, prepared)
                _write_json(
                    os.path.join(registry_root, report_id + ".json"),
                    {
                        "project_name": PRODUCTION_PROJECT_NAME,
                        "report_id": report_id,
                        "report_mode": LEGACY_STATIC,
                        "report_root": "reports",
                        "legacy_source_path": relative,
                        "legacy_source_sha256": source_sha256,
                    },
                )
                report_entries.append({
                    "legacy_source_path": relative,
                    "legacy_source_sha256": source_sha256,
                    "report_id": report_id,
                })
            else:
                _write_bytes(report_path, original)
                asset_path = os.path.join(assets_root, name)
                _write_bytes(asset_path, original)
                asset_names.append(relative)
        os.replace(temporary, output_root)
        temporary = ""
    finally:
        if temporary and os.path.isdir(temporary):
            shutil.rmtree(temporary)
    return {
        "status": "PASSED",
        "source_kind": "legacy_flat_root",
        "source_root": flat_root,
        "output_root": output_root,
        "release_identity": os.path.join(output_root, "release_identity.json"),
        "commit_sha": identity["commit_sha"],
        "project_name": PRODUCTION_PROJECT_NAME,
        "report_count": len(report_entries),
        "asset_count": len(asset_names),
        "reports": sorted(report_entries, key=lambda item: item["report_id"]),
        "assets": sorted(asset_names),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="prepare_legacy_flat_adoption.py"
    )
    parser.add_argument("--served-root", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--release-identity", required=True)
    parser.add_argument("--expected-commit-sha", required=True)
    args = parser.parse_args(argv)
    try:
        result = prepare_legacy_flat_adoption(
            args.served_root, args.output_root, args.release_identity,
            args.expected_commit_sha,
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
