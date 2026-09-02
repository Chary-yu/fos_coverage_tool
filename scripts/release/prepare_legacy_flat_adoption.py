"""Prepare a real legacy Served Root for explicit immutable adoption.

The historical production layout predates the immutable release contract.  It
keeps report HTML, LCOV/source-detail directories and static assets directly
under one directory, without ``reports/``, ``assets/`` or ``registry/``.

This tool creates a separate adoption staging tree.  Every source file is
copied to ``reports/<relative path>``; non-HTML files are also copied to
``assets/<relative path>``.  HTML receives only the identity fields that are
provable for a legacy static report.  Root-level reports get a deterministic
report id and registry entry; nested source/detail pages deliberately do not
receive fabricated scan, repository, file, Sidecar or asset identities.

The source directory is scanned before and after the copy.  The supplied
release identity is also bound to the actual root-level release-owned files
by path/size/SHA256 before an adoption can be committed.
"""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.candidate_artifact import PRODUCTION_PROJECT_NAME
from app.release_identity import is_valid_commit_sha
from app.reports.identity import LEGACY_STATIC, validate_report_id


ADOPTION_MANIFEST_VERSION = 1
ADOPTION_MANIFEST_NAME = "legacy_adoption_manifest.json"
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
    "validated_publication_identity.json", ADOPTION_MANIFEST_NAME,
))
_TOP_LEVEL_CONTROL_DIRECTORIES = frozenset((
    "CURRENT", "reports", "assets", "registry", ".source_cache",
))
_HTML_SUFFIXES = (".html", ".htm")
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
            raise ValueError(
                "legacy release identity asset path is invalid: {}".format(relative)
            )
        if relative in seen:
            raise ValueError("legacy release identity has duplicate asset: {}".format(relative))
        seen.add(relative)
        try:
            size = int(item.get("size"))
        except (TypeError, ValueError):
            raise ValueError(
                "legacy release identity asset size is invalid: {}".format(relative)
            )
        sha256 = str(item.get("sha256") or "")
        if size < 0 or not _SHA256_RE.fullmatch(sha256):
            raise ValueError(
                "legacy release identity asset fingerprint is invalid: {}".format(relative)
            )
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


def _add_legacy_identity_meta(raw, report_id=None, relative_path=""):
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
        raise ValueError(
            "legacy HTML must contain a complete <head>: {}".format(
                relative_path or "unknown"
            )
        )
    lines = [
        '<meta name="coverage-project" content="{}">'.format(
            PRODUCTION_PROJECT_NAME
        ),
        '<meta name="coverage-report-mode" content="{}">'.format(LEGACY_STATIC),
    ]
    if report_id:
        lines.append(
            '<meta name="coverage-report-id" content="{}">'.format(report_id)
        )
    additions = ("\n" + "\n".join(lines)).encode("ascii")
    return raw[:opening.end()] + additions + raw[opening.end():]


def _source_entry(flat_root, relative_path, path, file_stat):
    return {
        "path": relative_path.replace(os.sep, "/"),
        "absolute_path": path,
        "size": int(file_stat.st_size),
        "sha256": _sha256(path),
        "is_html": relative_path.lower().endswith(_HTML_SUFFIXES),
    }


def _scan_source_tree(flat_root):
    """Return every regular source file while rejecting links/special files."""
    flat_root = _real(flat_root)
    if os.path.islink(flat_root) or not os.path.isdir(flat_root):
        raise ValueError("legacy Flat Root must be a real directory")
    entries = []

    def visit(directory, relative_directory=""):
        try:
            names = sorted(os.listdir(directory))
        except OSError as exc:
            raise ValueError("legacy Flat Root cannot be scanned: {}".format(exc))
        for name in names:
            path = os.path.join(directory, name)
            relative = name if not relative_directory else os.path.join(
                relative_directory, name
            )
            if os.path.islink(path):
                raise ValueError(
                    "legacy Flat Root may not contain symlinks: {}".format(path)
                )
            try:
                file_stat = os.stat(path, follow_symlinks=False)
            except OSError as exc:
                raise ValueError(
                    "legacy Flat Root entry cannot be inspected: {}: {}".format(
                        path, exc
                    )
                )
            mode = file_stat.st_mode
            if stat.S_ISDIR(mode):
                if not relative_directory and name in _TOP_LEVEL_CONTROL_DIRECTORIES:
                    raise ValueError(
                        "legacy Flat Root already contains immutable layout entry: {}".format(
                            path
                        )
                    )
                if name == ".source_cache":
                    raise ValueError(
                        "legacy Flat Root may not contain .source_cache: {}".format(path)
                    )
                visit(path, relative)
            elif stat.S_ISREG(mode):
                if name in _CONTROL_NAMES:
                    raise ValueError(
                        "legacy Flat Root contains release control file: {}".format(
                            relative.replace(os.sep, "/")
                        )
                    )
                entries.append(_source_entry(flat_root, relative, path, file_stat))
            else:
                raise ValueError(
                    "legacy Flat Root may contain only regular files or directories: {}".format(
                        path
                    )
                )

    visit(flat_root)
    return sorted(entries, key=lambda item: item["path"])


def _source_manifest_entries(entries):
    return [
        {
            "path": entry["path"],
            "size": int(entry["size"]),
            "sha256": entry["sha256"],
        }
        for entry in sorted(entries, key=lambda item: item["path"])
    ]


def _source_tree_sha256(entries):
    return _canonical_hash(_source_manifest_entries(entries))


def _source_total_size(entries):
    return sum(int(entry["size"]) for entry in entries)


def _validate_flat_root(flat_root):
    """Validate and describe a recursive legacy Flat Root."""
    requested_root = os.path.abspath(str(flat_root))
    if os.path.islink(requested_root) or not os.path.isdir(requested_root):
        raise ValueError("legacy Flat Root must be a real directory")
    flat_root = _real(requested_root)
    entries = _scan_source_tree(flat_root)
    html_entries = [entry for entry in entries if entry["is_html"]]
    if not html_entries:
        raise ValueError("legacy Flat Root contains no HTML reports")
    if len(html_entries) == len(entries):
        raise ValueError("legacy Flat Root contains no Served static assets")
    for entry in html_entries:
        with open(entry["absolute_path"], "rb") as stream:
            raw = stream.read()
        existing = _identity_meta_names(raw)
        if existing:
            raise ValueError(
                "legacy Flat Root HTML must not already contain coverage identity metadata: {}".format(
                    entry["path"]
                )
            )
    return flat_root, entries, html_entries


def _bind_release_assets(entries, identity):
    """Bind every identity asset to an observed source file fingerprint."""
    by_path = {entry["path"]: entry for entry in entries}
    root_by_basename = {}
    for entry in entries:
        if "/" not in entry["path"]:
            root_by_basename.setdefault(os.path.basename(entry["path"]), []).append(entry)

    bindings = []
    for item in sorted(identity["asset_manifest"], key=lambda value: value["path"]):
        release_path = str(item["path"]).replace("\\", "/")
        source_entry = by_path.get(release_path)
        if source_entry is None:
            basename = os.path.basename(release_path)
            candidates = root_by_basename.get(basename) or []
            if not candidates:
                raise ValueError(
                    "legacy Flat Root is missing release asset alias: {}".format(
                        release_path
                    )
                )
            if len(candidates) != 1:
                raise ValueError(
                    "legacy Flat Root has ambiguous release asset alias: {}".format(
                        release_path
                    )
                )
            source_entry = candidates[0]
        expected_size = int(item["size"])
        expected_sha256 = str(item["sha256"]).lower()
        if int(source_entry["size"]) != expected_size or \
                source_entry["sha256"].lower() != expected_sha256:
            raise ValueError(
                "legacy Flat Root release asset does not match identity: {} from {}".format(
                    release_path, source_entry["path"]
                )
            )
        bindings.append({
            "release_asset_path": release_path,
            "source_path": source_entry["path"],
            "expected_size": expected_size,
            "expected_sha256": expected_sha256,
            "observed_size": int(source_entry["size"]),
            "observed_sha256": source_entry["sha256"],
        })
    return bindings


def _write_bytes(path, value):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "wb") as stream:
        stream.write(value)


def _write_json(path, value):
    _write_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
    )


def _adoption_manifest(flat_root, entries, identity, identity_path,
                       asset_bindings, modified_html, source_to_reports):
    source_files = _source_manifest_entries(entries)
    source_tree_sha256 = _source_tree_sha256(entries)
    source_file_count = len(source_files)
    source_total_size = _source_total_size(entries)
    scan = {
        "tree_sha256": source_tree_sha256,
        "file_count": source_file_count,
        "total_size": source_total_size,
    }
    return {
        "schema_version": ADOPTION_MANIFEST_VERSION,
        "source_kind": "legacy_flat_root",
        "source_root_realpath": flat_root,
        "source_tree_sha256": source_tree_sha256,
        "source_file_count": source_file_count,
        "source_total_size": source_total_size,
        "source_files": source_files,
        "source_scan": {
            "before": dict(scan),
            "after": dict(scan),
            "stable": True,
        },
        "release_identity_sha256": _canonical_hash(identity),
        "release_identity_file_sha256": _sha256(identity_path),
        "expected_commit_sha": identity["commit_sha"],
        "release_asset_bindings": asset_bindings,
        "source_to_reports": source_to_reports,
        "modified_html": sorted(
            modified_html, key=lambda item: item["source_path"]
        ),
    }


def prepare_legacy_flat_adoption(flat_root, output_root, release_identity_path,
                                 expected_commit_sha):
    """Create an isolated adoption tree from a recursive legacy release."""
    flat_root, entries, html_entries = _validate_flat_root(flat_root)
    output_root = os.path.abspath(str(output_root))
    if os.path.lexists(output_root):
        raise ValueError("legacy adoption output must not already exist")
    if _inside(flat_root, output_root) or _inside(output_root, flat_root):
        raise ValueError("legacy adoption output must be separate from Flat Root")
    requested_identity_path = os.path.abspath(str(release_identity_path))
    if os.path.islink(requested_identity_path) or not os.path.isfile(
            requested_identity_path):
        raise ValueError("legacy release identity file is missing or linked")
    identity_path = _real(requested_identity_path)
    identity = _validate_release_identity(identity_path, expected_commit_sha)
    asset_bindings = _bind_release_assets(entries, identity)
    source_before = _source_manifest_entries(entries)
    source_tree_before = _source_tree_sha256(entries)

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
    asset_paths = []
    modified_html = []
    source_to_reports = []
    root_html_paths = set(
        entry["path"] for entry in html_entries if "/" not in entry["path"]
    )
    try:
        # Keep the supplied identity bytes intact.  Adoption verifies it, but
        # must never regenerate or normalize historical release evidence.
        shutil.copyfile(
            identity_path,
            os.path.join(temporary, "release_identity.json"),
        )
        for entry in entries:
            relative = entry["path"]
            source_path = entry["absolute_path"]
            with open(source_path, "rb") as stream:
                original = stream.read()
            observed_size = len(original)
            observed_sha256 = hashlib.sha256(original).hexdigest()
            if observed_size != int(entry["size"]) or \
                    observed_sha256 != entry["sha256"]:
                raise ValueError(
                    "legacy Flat Root changed while it was being copied: {}".format(
                        relative
                    )
                )
            report_path = os.path.join(reports_root, *relative.split("/"))
            if entry["is_html"]:
                is_root_report = relative in root_html_paths
                report_id = _legacy_report_id(relative, entry["sha256"]) \
                    if is_root_report else ""
                prepared = _add_legacy_identity_meta(
                    original, report_id=report_id, relative_path=relative
                )
                _write_bytes(report_path, prepared)
                source_to_reports.append({
                    "source_path": relative,
                    "reports_path": "reports/" + relative,
                    "assets_path": "",
                    "report_scope": "root" if is_root_report else "nested",
                    "report_id": report_id,
                })
                modified_html.append({
                    "source_path": relative,
                    "reports_path": "reports/" + relative,
                    "report_id": report_id,
                    "before_size": observed_size,
                    "before_sha256": entry["sha256"],
                    "after_size": len(prepared),
                    "after_sha256": hashlib.sha256(prepared).hexdigest(),
                })
                report_entries.append({
                    "legacy_source_path": relative,
                    "legacy_source_sha256": entry["sha256"],
                    "report_id": report_id,
                    "report_scope": "root" if is_root_report else "nested",
                })
                if is_root_report:
                    _write_json(
                        os.path.join(registry_root, report_id + ".json"),
                        {
                            "project_name": PRODUCTION_PROJECT_NAME,
                            "report_id": report_id,
                            "report_mode": LEGACY_STATIC,
                            "report_root": "reports",
                            "legacy_source_path": relative,
                            "legacy_source_sha256": entry["sha256"],
                        },
                    )
            else:
                _write_bytes(report_path, original)
                asset_path = os.path.join(assets_root, *relative.split("/"))
                _write_bytes(asset_path, original)
                asset_paths.append(relative)
                source_to_reports.append({
                    "source_path": relative,
                    "reports_path": "reports/" + relative,
                    "assets_path": "assets/" + relative,
                    "report_scope": "static_asset",
                    "report_id": "",
                })

        # The source must be stable across the complete staging copy.  The
        # second scan also catches additions/removals and metadata changes.
        source_after_entries = _scan_source_tree(flat_root)
        source_after = _source_manifest_entries(source_after_entries)
        if source_before != source_after or \
                source_tree_before != _source_tree_sha256(source_after_entries):
            raise ValueError("legacy Flat Root changed during adoption")
        manifest = _adoption_manifest(
            flat_root, entries, identity, identity_path, asset_bindings,
            modified_html, source_to_reports,
        )
        _write_json(os.path.join(temporary, ADOPTION_MANIFEST_NAME), manifest)
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
        "adoption_manifest": os.path.join(output_root, ADOPTION_MANIFEST_NAME),
        "commit_sha": identity["commit_sha"],
        "project_name": PRODUCTION_PROJECT_NAME,
        "report_count": len(report_entries),
        "registry_count": len(root_html_paths),
        "asset_count": len(asset_paths),
        "reports": sorted(report_entries, key=lambda item: item["legacy_source_path"]),
        "assets": sorted(asset_paths),
        "source_tree_sha256": source_tree_before,
        "source_file_count": len(entries),
        "source_total_size": _source_total_size(entries),
        "release_identity_sha256": _canonical_hash(identity),
        "release_asset_bindings": asset_bindings,
        "modified_html": sorted(
            modified_html, key=lambda item: item["source_path"]
        ),
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
