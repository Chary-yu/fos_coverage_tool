#!/usr/bin/env python3
"""Runtime embedded by the R8 legacy Flat Root Candidate operator.

This module is copied verbatim into the standalone TXT operator.  It keeps
the production-side operation deliberately narrow: verify the pinned legacy
baseline, reconstruct two exact Git checkouts from embedded bundles, build an
isolated production Candidate with the offline-operator trust mode, and emit
compact evidence.  It never publishes, switches CURRENT, changes Nginx or
systemd, touches the application root, or changes a database.

The code is Python 3.6-compatible because the target release tooling has the
same compatibility requirement.
"""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import zipfile


SOURCE_SHA = "0f4009c9f8c253f42e60394841795522bbaaa24d"
SOURCE_TREE_SHA = "11a50aa52fdc388de5e776e4bc1fea477ca40fb9"
BASELINE_SHA = "e9fcc837a1ac9847f3966fc8ddb2aed92ca473fc"
BASELINE_ATTEMPT_ID = (
    "r8-legacy-baseline-e9fcc837-20260916T014723Z-3473778"
)
BASELINE_TREE_SHA256 = (
    "97ac0734d4f6b2a1cab924fe5c7d3b8c1e97f416b671d8ff14bcadec140fdff4"
)
BASELINE_MANIFEST_SHA256 = (
    "6e00b4f1103d498ab7d8a50e8c5772e0b3d8a04cac4ac863548432e0d6ede579"
)
ADOPTION_IDENTITY_SHA256 = (
    "090aa3e4a3cf75b76f510c6c4160afd07be9b275bf7d7599276db288f5dfe789"
)
EXPECTED_FLAT_ROOT = "/home/zcyu/coverage/export0810/onesensor"
EXPECTED_BASELINE_ROOT = (
    "/home/zcyu/fos_r8_baseline_exports/" + BASELINE_ATTEMPT_ID
)
EXPECTED_ADOPTION_IDENTITY = "adoption/release_identity.json"
EXPECTED_ADOPTION_SOURCE = "adoption-source"
EXPECTED_BASELINE_MANIFEST = "baseline-export-manifest.json"
EXPECTED_BASELINE_DIGEST = "baseline-export-manifest.sha256"
EXPECTED_COMPLETION_MARKER = "COMPLETION_MARKER"
EXPECTED_PRODUCTION_PUBLISH_ROOT = "/home/zcyu/coverage_published"
EXPECTED_CANDIDATE_PARENT = "/home/zcyu/coverage_candidate"
EXPECTED_REPOSITORY = "Chary-yu/fos_coverage_tool"
EXPECTED_HOST = "vfoswind"
EXPECTED_PROJECT = "FOS_V6R2"
EXPECTED_NGINX_CONFIG = "/etc/nginx/conf.d/coverage.conf"
EXPECTED_APPLICATION_MANIFEST_SHA256 = (
    "07e2e79a85da37e11eeb9bd2fca4b477f992250d66849df59fee6f4c8156a85e"
)
EXPECTED_APPLICATION_ASSET_HASH = (
    "8a4acc312e15583e877f10911b7b6ed8d301b2931cf777b650329888f04f1833"
)
EXPECTED_TOOLING_SHA = "@@R8_TOOLING_SHA@@"
EXPECTED_TOOLING_TREE = "@@R8_TOOLING_TREE@@"
EXPECTED_MARKER_BYTES = b"FOS_R8_FLAT_PRODUCTION_CANDIDATE_COMPLETE\n"
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SHA40_RE = re.compile(r"^[0-9a-fA-F]{40}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


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


def _canonical(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _load_json(path, label):
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError("{} is missing or linked: {}".format(label, path))
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("{} is unreadable: {}".format(label, exc))
    if not isinstance(value, dict):
        raise ValueError("{} must be a JSON object".format(label))
    return value


def _write_json(path, value):
    path = os.path.abspath(str(path))
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
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


def _write_bytes(path, value):
    path = os.path.abspath(str(path))
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    temporary = "{}.tmp-{}".format(path, os.getpid())
    with open(temporary, "wb") as stream:
        stream.write(value)
        stream.flush()
        try:
            os.fsync(stream.fileno())
        except OSError:
            pass
    os.replace(temporary, path)


def _require_regular(path, label):
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError("{} must be a regular file: {}".format(label, path))


def _require_real_directory(path, label):
    requested = os.path.abspath(str(path))
    if os.path.islink(requested) or not os.path.isdir(requested):
        raise ValueError("{} must be a real directory: {}".format(label, path))
    if _real(requested) != requested:
        raise ValueError("{} must not resolve through a link: {}".format(label, path))
    return requested


def _safe_relative(path):
    value = str(path or "").replace("\\", "/")
    return bool(value) and not value.startswith("/") and \
        value != "." and ".." not in value.split("/")


def inventory(root):
    """Return a complete regular-file and directory inventory."""
    root = _require_real_directory(root, "inventory root")
    files = []
    directories = []

    def visit(directory, relative_directory=""):
        try:
            names = sorted(os.listdir(directory))
        except OSError as exc:
            raise ValueError("cannot scan {}: {}".format(directory, exc))
        for name in names:
            path = os.path.join(directory, name)
            relative = name if not relative_directory else \
                relative_directory + "/" + name
            if os.path.islink(path):
                raise ValueError("symlink in inventory: {}".format(relative))
            try:
                file_stat = os.lstat(path)
            except OSError as exc:
                raise ValueError("cannot stat {}: {}".format(relative, exc))
            if stat.S_ISDIR(file_stat.st_mode):
                if not _safe_relative(relative):
                    raise ValueError("unsafe directory path: {}".format(relative))
                directories.append(relative.replace(os.sep, "/"))
                visit(path, relative)
            elif stat.S_ISREG(file_stat.st_mode):
                if not _safe_relative(relative):
                    raise ValueError("unsafe file path: {}".format(relative))
                files.append({
                    "path": relative.replace(os.sep, "/"),
                    "size": int(file_stat.st_size),
                    "sha256": _sha256(path),
                })
            else:
                raise ValueError("special file in inventory: {}".format(relative))

    visit(root)
    files.sort(key=lambda item: item["path"])
    directories.sort()
    return {
        "files": files,
        "directories": directories,
        "file_count": len(files),
        "total_size": sum(int(item["size"]) for item in files),
        "tree_sha256": _canonical(files),
    }


def _normalized_inventory(value, label):
    if not isinstance(value, dict):
        raise ValueError("{} is not an inventory object".format(label))
    required = ("files", "directories", "file_count", "total_size", "tree_sha256")
    missing = [key for key in required if key not in value]
    if missing:
        raise ValueError("{} is missing {}".format(label, ",".join(missing)))
    if not isinstance(value["files"], list) or \
            not isinstance(value["directories"], list):
        raise ValueError("{} file/directory lists are invalid".format(label))
    files = []
    seen = set()
    total = 0
    for item in value["files"]:
        if not isinstance(item, dict):
            raise ValueError("{} contains an invalid file record".format(label))
        path = str(item.get("path") or "").replace("\\", "/")
        if not _safe_relative(path) or path in seen:
            raise ValueError("{} contains an invalid/duplicate file path".format(label))
        seen.add(path)
        try:
            size = int(item.get("size"))
        except (TypeError, ValueError):
            raise ValueError("{} contains an invalid file size".format(label))
        digest = str(item.get("sha256") or "").lower()
        if size < 0 or not SHA256_RE.fullmatch(digest):
            raise ValueError("{} contains an invalid file fingerprint".format(label))
        files.append({"path": path, "size": size, "sha256": digest})
        total += size
    if files != sorted(files, key=lambda item: item["path"]):
        raise ValueError("{} file order is not canonical".format(label))
    directories = [str(item).replace("\\", "/") for item in value["directories"]]
    if any(not _safe_relative(item) for item in directories) or \
            directories != sorted(set(directories)):
        raise ValueError("{} directory order is invalid".format(label))
    try:
        file_count = int(value["file_count"])
        total_size = int(value["total_size"])
    except (TypeError, ValueError):
        raise ValueError("{} inventory accounting is invalid".format(label))
    tree = str(value.get("tree_sha256") or "").lower()
    if file_count != len(files) or total_size != total or \
            not SHA256_RE.fullmatch(tree) or _canonical(files) != tree:
        raise ValueError("{} inventory accounting does not reconcile".format(label))
    return {
        "files": files,
        "directories": directories,
        "file_count": file_count,
        "total_size": total_size,
        "tree_sha256": tree,
    }


def _compare_inventory(left, right, label):
    normalized_left = _normalized_inventory(left, label + " left")
    normalized_right = _normalized_inventory(right, label + " right")
    if normalized_left != normalized_right:
        raise ValueError("{} mismatch".format(label))
    return normalized_left


def _validate_exact_sha(value, label, pattern):
    value = str(value or "").strip().lower()
    if not pattern.fullmatch(value) or value == "0" * len(value):
        raise ValueError("{} is not an exact SHA".format(label))
    return value


def _asset_bindings(flat_inventory, identity):
    files = {item["path"]: item for item in flat_inventory["files"]}
    by_basename = {}
    for item in flat_inventory["files"]:
        if "/" not in item["path"]:
            by_basename.setdefault(os.path.basename(item["path"]), []).append(item)
    result = []
    declared = identity.get("asset_manifest")
    if not isinstance(declared, list) or not declared:
        raise ValueError("legacy adoption identity asset_manifest is invalid")
    for item in sorted(declared, key=lambda value: str(value.get("path") or "")):
        relative = str(item.get("path") or "").replace("\\", "/")
        if not _safe_relative(relative):
            raise ValueError("legacy asset path is invalid: {}".format(relative))
        source = files.get(relative)
        if source is None:
            candidates = by_basename.get(os.path.basename(relative), [])
            if len(candidates) != 1:
                raise ValueError("legacy asset alias is missing or ambiguous: {}".format(relative))
            source = candidates[0]
        expected_size = int(item.get("size"))
        expected_sha = str(item.get("sha256") or "").lower()
        if int(source["size"]) != expected_size or source["sha256"].lower() != expected_sha:
            raise ValueError("legacy asset bytes do not match identity: {}".format(relative))
        result.append({
            "release_asset_path": relative,
            "source_path": source["path"],
            "expected_size": expected_size,
            "expected_sha256": expected_sha,
            "observed_size": int(source["size"]),
            "observed_sha256": source["sha256"],
        })
    return result


def _expected_flat_binding(flat_root, flat_inventory, identity):
    assets = []
    for item in identity["asset_manifest"]:
        assets.append({
            "path": str(item["path"]).replace("\\", "/"),
            "size": int(item["size"]),
            "sha256": str(item["sha256"]).lower(),
        })
    return {
        "source_kind": "legacy_flat_root",
        "previous_release_commit_sha": BASELINE_SHA,
        "legacy_source_root_realpath": _real(flat_root),
        "legacy_source_tree_sha256": flat_inventory["tree_sha256"],
        "legacy_source_file_count": flat_inventory["file_count"],
        "legacy_source_total_size": flat_inventory["total_size"],
        "legacy_release_identity_sha256": _canonical(identity),
        "source_files": flat_inventory["files"],
        "release_asset_bindings": _asset_bindings(flat_inventory, identity),
        "binding_status": "PASSED",
    }


def _expected_flat_binding_with_identity_path(flat_root, flat_inventory,
                                              identity, identity_path):
    binding = _expected_flat_binding(flat_root, flat_inventory, identity)
    binding["legacy_release_identity_file_sha256"] = _sha256(identity_path)
    return binding


def _validate_adoption_identity(identity, identity_path, adoption_inventory):
    if identity.get("schema_version") != 2 or \
            identity.get("identity_mode") != "LEGACY_PRE_ADOPTION" or \
            identity.get("identity_origin") != "legacy_adoption":
        raise ValueError("legacy adoption identity mode/origin is invalid")
    exact_fields = {
        "original_release_identity": "ABSENT",
        "historical_build_attestation": "NOT_AVAILABLE",
        "artifact_role": "validation_input_baseline_only",
        "project_name": EXPECTED_PROJECT,
        "commit_sha": BASELINE_SHA,
        "expected_baseline_sha": BASELINE_SHA,
        "application_identity_mode": "PACKAGED_RELEASE_MANIFEST",
        "application_git_sha": "NOT_AVAILABLE_PACKAGED_DEPLOYMENT",
        "application_git_tree_sha": "NOT_AVAILABLE_PACKAGED_DEPLOYMENT",
        "production_publishable": False,
    }
    for key, expected in exact_fields.items():
        if identity.get(key) != expected:
            raise ValueError("legacy adoption identity {} is invalid".format(key))
    anchor = identity.get("packaged_release_anchor") or {}
    if anchor.get("verified") is not True or \
            anchor.get("commit_sha") != BASELINE_SHA or \
            anchor.get("release_manifest_sha256") != EXPECTED_APPLICATION_MANIFEST_SHA256 or \
            anchor.get("asset_hash") != EXPECTED_APPLICATION_ASSET_HASH or \
            anchor.get("asset_manifest_hash") != EXPECTED_APPLICATION_ASSET_HASH:
        raise ValueError("packaged application release anchor is invalid")
    assets = identity.get("asset_manifest")
    if not isinstance(assets, list) or len(assets) != 20131:
        raise ValueError("legacy adoption identity asset_count is invalid")
    normalized_assets = []
    seen = set()
    for item in assets:
        if not isinstance(item, dict):
            raise ValueError("legacy adoption identity asset record is invalid")
        path = str(item.get("path") or "").replace("\\", "/")
        if not _safe_relative(path) or path in seen:
            raise ValueError("legacy adoption identity asset path is invalid")
        seen.add(path)
        size = int(item.get("size"))
        digest = str(item.get("sha256") or "").lower()
        if size < 0 or not SHA256_RE.fullmatch(digest):
            raise ValueError("legacy adoption identity asset fingerprint is invalid")
        normalized_assets.append({"path": path, "size": size, "sha256": digest})
    if normalized_assets != adoption_inventory["files"]:
        raise ValueError("adoption identity does not match adoption-source inventory")
    if identity.get("asset_count") != len(normalized_assets) or \
            identity.get("asset_hash") != BASELINE_TREE_SHA256 or \
            identity.get("asset_manifest_hash") != BASELINE_TREE_SHA256 or \
            identity.get("asset_manifest_version") != 1 or \
            _canonical(normalized_assets) != BASELINE_TREE_SHA256:
        raise ValueError("legacy adoption identity asset contract is invalid")
    for key, expected in (
            ("snapshot_tree_sha256", BASELINE_TREE_SHA256),
            ("adoption_source_tree_sha256", BASELINE_TREE_SHA256)):
        if identity.get(key) != expected:
            raise ValueError("legacy adoption identity {} is invalid".format(key))


def validate_baseline(baseline_root):
    """Verify the production-resident compactly evidenced baseline inputs."""
    baseline_root = _require_real_directory(baseline_root, "baseline root")
    if baseline_root != EXPECTED_BASELINE_ROOT:
        raise ValueError("baseline root path is not the pinned attempt directory")
    manifest_path = os.path.join(baseline_root, EXPECTED_BASELINE_MANIFEST)
    digest_path = os.path.join(baseline_root, EXPECTED_BASELINE_DIGEST)
    marker_path = os.path.join(baseline_root, EXPECTED_COMPLETION_MARKER)
    _require_regular(manifest_path, "baseline export manifest")
    _require_regular(digest_path, "baseline manifest digest")
    _require_regular(marker_path, "baseline completion marker")
    manifest_sha = _sha256(manifest_path)
    if manifest_sha != BASELINE_MANIFEST_SHA256:
        raise ValueError("baseline export manifest SHA256 mismatch")
    with open(digest_path, "rb") as stream:
        if stream.read() != (manifest_sha + "  " + EXPECTED_BASELINE_MANIFEST + "\n").encode("ascii"):
            raise ValueError("baseline manifest digest sidecar is invalid")
    with open(marker_path, "rb") as stream:
        if stream.read() != b"FOS_R8_BASELINE_EXPORT_COMPLETE\n":
            raise ValueError("baseline completion marker is invalid")
    manifest = _load_json(manifest_path, "baseline export manifest")
    required = (
        "export_schema_version", "schema_version", "status", "attempt_id",
        "tool_filename", "tool_sha256", "tool_size", "hostname",
        "collection_timestamp_utc", "expected_baseline_sha",
        "resolved_nginx_config_paths", "resolved_nginx_config_sha256",
        "resolved_served_root", "source_before_inventory",
        "source_after_inventory", "copied_snapshot_inventory",
        "sidecar_summary", "copied_snapshot_root_relative_path",
        "source_file_count", "source_total_size",
        "source_canonical_tree_sha256", "sanitization", "completion_marker",
        "manifest_digest",
    )
    missing = [key for key in required if key not in manifest]
    if missing:
        raise ValueError("baseline export manifest is missing {}".format(
            ",".join(missing)
        ))
    if manifest.get("export_schema_version") != 1 or \
            manifest.get("schema_version") != 1 or manifest.get("status") != "PASS":
        raise ValueError("baseline export manifest schema/status is invalid")
    if manifest.get("attempt_id") != BASELINE_ATTEMPT_ID or \
            manifest.get("hostname") != EXPECTED_HOST or \
            manifest.get("expected_baseline_sha") != BASELINE_SHA:
        raise ValueError("baseline export identity is invalid")
    if manifest.get("resolved_served_root") != EXPECTED_FLAT_ROOT or \
            manifest.get("resolved_nginx_config_paths") != EXPECTED_NGINX_CONFIG or \
            not SHA256_RE.fullmatch(str(manifest.get("resolved_nginx_config_sha256") or "")):
        raise ValueError("baseline production topology evidence is invalid")
    if manifest.get("copied_snapshot_root_relative_path") != "legacy-flat-root":
        raise ValueError("baseline snapshot path is invalid")
    completion = manifest.get("completion_marker") or {}
    if completion.get("path") != EXPECTED_COMPLETION_MARKER or \
            completion.get("written_last") is not True or \
            completion.get("required") is not True or \
            completion.get("sha256") != _sha256(marker_path):
        raise ValueError("baseline completion contract is invalid")
    digest = manifest.get("manifest_digest") or {}
    if digest.get("path") != EXPECTED_BASELINE_DIGEST or \
            digest.get("written_after_manifest") is not True:
        raise ValueError("baseline manifest digest contract is invalid")
    sanitization = manifest.get("sanitization") or {}
    if sanitization.get("status") != "PASS" or \
            sanitization.get("secret_values_copied") != "NONE":
        raise ValueError("baseline sanitization contract is invalid")
    sidecar = manifest.get("sidecar_summary") or {}
    if sidecar.get("SIDECAR_LAYOUT") != "reports/.source_cache/**/meta.json":
        raise ValueError("baseline Sidecar summary is invalid")

    source_before = _normalized_inventory(
        manifest["source_before_inventory"], "baseline source-before"
    )
    source_after = _normalized_inventory(
        manifest["source_after_inventory"], "baseline source-after"
    )
    copied = _normalized_inventory(
        manifest["copied_snapshot_inventory"], "baseline copied snapshot"
    )
    _compare_inventory(source_before, source_after, "baseline source before/after")
    _compare_inventory(source_before, copied, "baseline source/copy")
    if source_before["file_count"] != 20131 or \
            source_before["total_size"] != 1854259212 or \
            source_before["tree_sha256"] != BASELINE_TREE_SHA256 or \
            manifest.get("source_file_count") != 20131 or \
            manifest.get("source_total_size") != 1854259212 or \
            manifest.get("source_canonical_tree_sha256") != BASELINE_TREE_SHA256:
        raise ValueError("baseline source inventory does not match pinned identity")

    flat_root = os.path.join(baseline_root, "legacy-flat-root")
    flat_inventory = inventory(flat_root)
    _compare_inventory(copied, flat_inventory, "production Flat Root")
    adoption_inventory_path = os.path.join(
        baseline_root, "evidence", "inventory-adoption-source.json"
    )
    adoption_inventory = _normalized_inventory(
        _load_json(adoption_inventory_path, "adoption-source inventory"),
        "adoption-source evidence",
    )
    actual_adoption = inventory(os.path.join(
        baseline_root, EXPECTED_ADOPTION_SOURCE
    ))
    _compare_inventory(adoption_inventory, actual_adoption, "adoption-source bytes")
    if adoption_inventory != flat_inventory:
        raise ValueError("Flat Root and adoption-source inventories differ")

    identity_path = os.path.join(baseline_root, EXPECTED_ADOPTION_IDENTITY)
    _require_regular(identity_path, "legacy adoption release identity")
    identity_sha = _sha256(identity_path)
    if identity_sha != ADOPTION_IDENTITY_SHA256:
        raise ValueError("legacy adoption release identity SHA256 mismatch")
    identity = _load_json(identity_path, "legacy adoption release identity")
    _validate_adoption_identity(identity, identity_path, adoption_inventory)
    evidence_identity = manifest.get("identity_evidence") or {}
    if evidence_identity:
        if evidence_identity.get("identity_mode") != "LEGACY_PRE_ADOPTION" or \
                evidence_identity.get("identity_file_sha256") != identity_sha:
            raise ValueError("baseline identity evidence does not match adoption identity")
    binding = _expected_flat_binding_with_identity_path(
        flat_root, flat_inventory, identity, identity_path
    )
    if binding["legacy_source_tree_sha256"] != BASELINE_TREE_SHA256 or \
            binding["legacy_release_identity_file_sha256"] != identity_sha:
        raise ValueError("baseline Flat source binding is invalid")
    return {
        "status": "PASSED",
        "baseline_root": baseline_root,
        "flat_root": flat_root,
        "identity_path": identity_path,
        "manifest_path": manifest_path,
        "manifest_sha256": manifest_sha,
        "identity_sha256": identity_sha,
        "flat_inventory": flat_inventory,
        "adoption_inventory": adoption_inventory,
        "flat_source_binding": binding,
        "manifest": manifest,
        "identity": identity,
    }


def _run(argv, cwd=None, label="command"):
    try:
        process = subprocess.Popen(
            list(argv), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, stderr = process.communicate()
    except (OSError, ValueError) as exc:
        raise ValueError("{} could not start: {}".format(label, exc))
    output = stdout.decode("utf-8", "replace")
    error = stderr.decode("utf-8", "replace")
    if process.returncode != 0:
        raise ValueError("{} failed ({}): {}".format(
            label, process.returncode, error.strip() or output.strip()
        ))
    return output, error


def _verify_and_clone_bundle(bundle_path, expected_sha, expected_commit,
                             expected_tree, destination, label):
    _require_regular(bundle_path, label + " bundle")
    if _sha256(bundle_path).lower() != str(expected_sha).lower():
        raise ValueError("{} bundle SHA256 mismatch".format(label))
    with tempfile.TemporaryDirectory(prefix="r8-flat-bundle-verify-") as root:
        verify_repo = os.path.join(root, "verify")
        os.makedirs(verify_repo)
        _run(("git", "init", "-q"), cwd=verify_repo, label=label + " bundle init")
        _run(("git", "bundle", "verify", os.path.abspath(bundle_path)),
             cwd=verify_repo, label=label + " bundle verify")
    if os.path.lexists(destination):
        raise ValueError("{} checkout destination already exists".format(label))
    _run(("git", "clone", "-q", os.path.abspath(bundle_path), destination),
         label=label + " bundle clone")
    _run(("git", "checkout", "--detach", "-q", expected_commit),
         cwd=destination, label=label + " exact checkout")
    observed_sha, _ = _run(("git", "rev-parse", "HEAD"), cwd=destination,
                           label=label + " HEAD")
    observed_tree, _ = _run(("git", "rev-parse", "HEAD^{tree}"), cwd=destination,
                            label=label + " tree")
    if observed_sha.strip().lower() != expected_commit.lower() or \
            observed_tree.strip().lower() != expected_tree.lower():
        raise ValueError("{} bundle checkout identity mismatch".format(label))
    dirty, _ = _run(("git", "status", "--porcelain", "--untracked-files=all"),
                    cwd=destination, label=label + " checkout status")
    if dirty.strip():
        raise ValueError("{} bundle checkout is dirty".format(label))
    _run(("git", "fsck", "--full", "--strict"), cwd=destination,
         label=label + " checkout fsck")


def _verify_tool_contract(tooling_root):
    builder = os.path.join(
        tooling_root, "scripts", "release",
        "build_flat_production_candidate_artifact.py",
    )
    adoption = os.path.join(
        tooling_root, "scripts", "release", "prepare_legacy_flat_adoption.py"
    )
    if not os.path.isfile(builder) or not os.path.isfile(adoption):
        raise ValueError("pinned tooling bundle lacks the Flat Root builder contract")
    with open(builder, "r", encoding="utf-8") as stream:
        builder_text = stream.read()
    with open(adoption, "r", encoding="utf-8") as stream:
        adoption_text = stream.read()
    required_builder_markers = (
        "prepare_legacy_flat_adoption", "flat_source_binding",
        "verify_flat_source_binding", "RELEASE_TRUST_MODE_OFFLINE_OPERATOR",
        "offline_operator_evidence_output", "candidate_build_attestation",
    )
    for marker in required_builder_markers:
        if marker not in builder_text:
            raise ValueError("Flat Root builder contract marker is missing: {}".format(marker))
    for marker in ("source_scan", "source_before", "source_after", "os.replace"):
        if marker not in adoption_text:
            raise ValueError("Flat Root adoption contract marker is missing: {}".format(marker))
    sys.path.insert(0, tooling_root)
    from app.candidate_artifact import (  # pylint: disable=import-outside-toplevel
        PRODUCTION_RELEASE_ARTIFACT_ROLE, RELEASE_TRUST_MODE_OFFLINE_OPERATOR,
        PRODUCTION_PROJECT_NAME,
    )
    from app.release_publication import (  # pylint: disable=import-outside-toplevel
        ImmutableReleasePublisher,
    )
    if PRODUCTION_RELEASE_ARTIFACT_ROLE != "production_candidate" or \
            RELEASE_TRUST_MODE_OFFLINE_OPERATOR != "offline_operator" or \
            PRODUCTION_PROJECT_NAME != EXPECTED_PROJECT:
        raise ValueError("pinned tooling artifact contract is not canonical")
    publication_text_path = os.path.join(tooling_root, "app", "release_publication.py")
    with open(publication_text_path, "r", encoding="utf-8") as stream:
        publication_text = stream.read()
    if "verify_offline_operator_trust" not in publication_text or \
            "RELEASE_TRUST_MODE_OFFLINE_OPERATOR" not in publication_text or \
            "offline operator publication requires" not in publication_text:
        raise ValueError("final publication path lacks offline operator trust")
    if not hasattr(ImmutableReleasePublisher, "prepare"):
        raise ValueError("final publication publisher contract is missing")


def self_test(source_bundle, tooling_bundle, source_root, tooling_root,
              source_bundle_sha256, tooling_bundle_sha256):
    _verify_and_clone_bundle(
        source_bundle, source_bundle_sha256, SOURCE_SHA, SOURCE_TREE_SHA,
        source_root, "application source"
    )
    _verify_and_clone_bundle(
        tooling_bundle, tooling_bundle_sha256, EXPECTED_TOOLING_SHA,
        EXPECTED_TOOLING_TREE, tooling_root,
        "release tooling"
    )
    _verify_tool_contract(tooling_root)
    print("SELF_TEST_SOURCE_BUNDLE=PASS")
    print("SELF_TEST_TOOLING_BUNDLE=PASS")
    print("SELF_TEST_FLAT_BUILDER_CONTRACT=PASS")
    print("SELF_TEST_OFFLINE_PUBLICATION_CONTRACT=PASS")
    print("SELF_TEST_PRODUCTION_MUTATION=NONE")
    return 0


def _check_hostname():
    observed = str(socket.gethostname() or "").split(".", 1)[0]
    if observed != EXPECTED_HOST:
        raise ValueError("operator must run on hostname={}".format(EXPECTED_HOST))


def _check_candidate_state(state_root, session_id):
    if not SAFE_ID_RE.fullmatch(session_id):
        raise ValueError("candidate validation session id is invalid")
    basename = os.path.basename(os.path.normpath(state_root))
    if not basename.startswith("r8-production-candidate-0f4009c9-"):
        raise ValueError("candidate state root is outside the pinned pattern")
    if not _inside(EXPECTED_CANDIDATE_PARENT, state_root):
        raise ValueError("candidate state root is outside the isolated candidate parent")
    if os.path.lexists(os.path.join(state_root, "candidate")) or \
            os.path.lexists(os.path.join(state_root, ".candidate-staging")):
        raise ValueError("candidate destination contains ambiguous state")
    for relative in (
            "release_identity.json", "offline_operator_evidence.json",
            "flat-source-binding.json", "build_result.json",
            "candidate_build_failure.json", "state.json",
            "r8-flat-candidate-evidence.zip", "CANDIDATE_COMPLETE",
            "evidence"):
        if os.path.lexists(os.path.join(state_root, relative)):
            raise ValueError("candidate state contains ambiguous {}".format(relative))


def _check_disk_space(state_root, flat_inventory):
    flat_bytes = int(flat_inventory["total_size"])
    state_parent = os.path.dirname(os.path.abspath(state_root))
    temporary_root = tempfile.gettempdir()
    required_state = flat_bytes * 2 + 1024 * 1024 * 1024
    required_temporary = flat_bytes + 512 * 1024 * 1024
    state_free = shutil.disk_usage(state_parent).free
    temporary_free = shutil.disk_usage(temporary_root).free
    if state_free < required_state:
        raise ValueError("candidate filesystem has insufficient free space")
    if temporary_free < required_temporary:
        raise ValueError("temporary filesystem has insufficient free space")
    return {
        "state_free_bytes": int(state_free),
        "state_required_bytes": int(required_state),
        "temporary_free_bytes": int(temporary_free),
        "temporary_required_bytes": int(required_temporary),
    }


def _candidate_inventory_summary(candidate_root, manifest):
    entries = manifest.get("files") or []
    return {
        "status": "PASSED",
        "candidate_root": _real(candidate_root),
        "file_count": len(entries),
        "total_size": sum(int(item["size"]) for item in entries),
        "artifact_sha256": manifest.get("artifact_sha256"),
        "reports_sha256": manifest.get("reports_sha256"),
        "assets_sha256": manifest.get("assets_sha256"),
        "registry_sha256": manifest.get("registry_sha256"),
        "manifest_file_count": len(entries),
        "artifact_role": manifest.get("artifact_role"),
        "production_publishable": manifest.get("production_publishable"),
        "project_name": manifest.get("project_name"),
        "source_commit_sha": manifest.get("source_commit_sha"),
        "source_tree_sha": manifest.get("source_tree_sha"),
        "release_trust_mode": "offline_operator",
    }


def _copy_evidence_file(source, target):
    _require_regular(source, "evidence source")
    if os.path.lexists(target):
        raise ValueError("evidence destination already exists: {}".format(target))
    parent = os.path.dirname(target)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    shutil.copyfile(source, target)


def _write_compact_evidence(state_root, candidate_root, identity_path,
                            manifest_path, attestation_path, offline_path,
                            binding, result, operator_sha, session_id,
                            source_bundle, tooling_bundle, space):
    evidence_root = os.path.join(state_root, "evidence")
    if os.path.lexists(evidence_root):
        raise ValueError("compact evidence destination already exists")
    os.makedirs(evidence_root)
    _copy_evidence_file(
        manifest_path, os.path.join(evidence_root, "candidate_artifact_manifest.json")
    )
    _copy_evidence_file(
        attestation_path,
        os.path.join(evidence_root, "candidate_build_attestation.json"),
    )
    _copy_evidence_file(
        identity_path, os.path.join(evidence_root, "release_identity.json")
    )
    _copy_evidence_file(
        offline_path,
        os.path.join(evidence_root, "offline_operator_evidence.json"),
    )
    _write_json(os.path.join(evidence_root, "flat_source_binding.json"), binding)
    _write_json(os.path.join(evidence_root, "build_result.json"), result)
    manifest = _load_json(manifest_path, "candidate artifact manifest")
    summary = _candidate_inventory_summary(candidate_root, manifest)
    _write_json(os.path.join(evidence_root, "candidate_inventory_summary.json"), summary)
    _write_json(os.path.join(evidence_root, "attempt_identity.json"), {
        "schema_version": 1,
        "source_sha": SOURCE_SHA,
        "source_tree_sha": SOURCE_TREE_SHA,
        "source_bundle_sha256": _sha256(source_bundle),
        "tooling_bundle_sha256": _sha256(tooling_bundle),
        "operator_sha256": operator_sha,
        "baseline_sha": BASELINE_SHA,
        "baseline_attempt_id": BASELINE_ATTEMPT_ID,
        "baseline_tree_sha256": BASELINE_TREE_SHA256,
        "baseline_manifest_sha256": BASELINE_MANIFEST_SHA256,
        "adoption_identity_sha256": ADOPTION_IDENTITY_SHA256,
        "validation_session_id": session_id,
        "candidate_artifact_sha256": manifest.get("artifact_sha256"),
        "release_trust_mode": "offline_operator",
    })
    _write_bytes(
        os.path.join(evidence_root, "candidate_artifact_sha256.txt"),
        (str(manifest.get("artifact_sha256")) + "  candidate\n").encode("ascii"),
    )
    _write_bytes(
        os.path.join(evidence_root, "source_bundle.sha256"),
        (_sha256(source_bundle) + "  source.bundle\n").encode("ascii"),
    )
    _write_bytes(
        os.path.join(evidence_root, "operator.sha256"),
        (str(operator_sha) + "  operator.txt\n").encode("ascii"),
    )
    _write_json(os.path.join(evidence_root, "disk_preflight.json"), space)
    zip_path = os.path.join(state_root, "r8-flat-candidate-evidence.zip")
    zip_temporary = zip_path + ".tmp-{}".format(os.getpid())
    with zipfile.ZipFile(zip_temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for directory, _dirnames, filenames in os.walk(evidence_root, followlinks=False):
            for name in sorted(filenames):
                path = os.path.join(directory, name)
                if os.path.islink(path) or not os.path.isfile(path):
                    raise ValueError("compact evidence contains a non-regular file")
                archive.write(path, os.path.relpath(path, evidence_root))
    os.replace(zip_temporary, zip_path)
    return evidence_root, zip_path


def _cleanup_partial(state_root, staging_root, candidate_root):
    for path in (staging_root, candidate_root):
        if not path or not os.path.lexists(path) or not _inside(state_root, path):
            continue
        if os.path.islink(path):
            os.unlink(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)


def _record_failure(state_root, session_id, phase, exc):
    try:
        _write_json(os.path.join(state_root, "candidate_build_failure.json"), {
            "schema_version": 1,
            "status": "BLOCKED",
            "phase": phase,
            "validation_session_id": session_id,
            "source_sha": SOURCE_SHA,
            "source_tree_sha": SOURCE_TREE_SHA,
            "baseline_sha": BASELINE_SHA,
            "baseline_attempt_id": BASELINE_ATTEMPT_ID,
            "error_type": type(exc).__name__,
            "error": str(exc),
        })
    except Exception:
        pass


def build(args):
    state_root = os.path.abspath(args.state_root)
    session_id = str(args.session_id or "").strip()
    phase = "operator preflight"
    staging_root = os.path.join(state_root, ".candidate-staging")
    candidate_root = os.path.join(state_root, "candidate")
    try:
        _check_hostname()
        if state_root != _real(state_root):
            raise ValueError("candidate state root must not be a symlink")
        _check_candidate_state(state_root, session_id)
        if os.path.lexists(os.path.join(EXPECTED_PRODUCTION_PUBLISH_ROOT, "CURRENT")):
            raise ValueError("CURRENT exists; legacy Flat Root first-migration path is not applicable")
        if not os.path.isdir(EXPECTED_CANDIDATE_PARENT):
            os.makedirs(EXPECTED_CANDIDATE_PARENT)
        if os.path.islink(EXPECTED_CANDIDATE_PARENT):
            raise ValueError("candidate parent must not be a symlink")
        baseline = validate_baseline(EXPECTED_BASELINE_ROOT)
        space = _check_disk_space(state_root, baseline["flat_inventory"])
        if os.path.lexists(staging_root) or os.path.lexists(candidate_root):
            raise ValueError("candidate destination contains ambiguous state")
        _write_json(os.path.join(state_root, "state.json"), {
            "schema_version": 1,
            "status": "IN_PROGRESS",
            "validation_session_id": session_id,
            "source_sha": SOURCE_SHA,
            "source_tree_sha": SOURCE_TREE_SHA,
            "baseline_attempt_id": BASELINE_ATTEMPT_ID,
        })

        phase = "embedded bundle verification"
        source_root = os.path.join(state_root, "source")
        tooling_root = os.path.join(state_root, "tooling")
        _verify_and_clone_bundle(
            args.source_bundle, args.source_bundle_sha256, SOURCE_SHA,
            SOURCE_TREE_SHA, source_root, "application source"
        )
        _verify_and_clone_bundle(
            args.tooling_bundle, args.tooling_bundle_sha256,
            EXPECTED_TOOLING_SHA, EXPECTED_TOOLING_TREE, tooling_root,
            "release tooling"
        )
        _verify_tool_contract(tooling_root)

        phase = "Flat source binding preflight"
        binding_path = os.path.join(state_root, "flat-source-binding.json")
        _write_json(binding_path, baseline["flat_source_binding"])
        source_bundle = _real(args.source_bundle)
        identity_path = baseline["identity_path"]
        offline_path = os.path.join(state_root, "offline_operator_evidence.json")
        identity_output = os.path.join(state_root, "release_identity.json")
        result_output = os.path.join(state_root, "build_result.json")
        phase = "isolated Flat Root Candidate build"
        builder = os.path.join(
            tooling_root, "scripts", "release",
            "build_flat_production_candidate_artifact.py",
        )
        command = [
            sys.executable, builder,
            "--flat-served-root", baseline["flat_root"],
            "--flat-release-identity", identity_path,
            "--source-repo-root", source_root,
            "--production-candidate-root", staging_root,
            "--release-identity-output", identity_output,
            "--expected-previous-release-sha", BASELINE_SHA,
            "--expected-source-binding", binding_path,
            "--release-trust-mode", "offline_operator",
            "--offline-operator-evidence-output", offline_path,
            "--offline-operator-source-bundle", source_bundle,
            "--offline-operator-repository", EXPECTED_REPOSITORY,
            "--production-host", EXPECTED_HOST,
            "--production-baseline-sha", BASELINE_SHA,
            "--validation-session-id", session_id,
            "--flat-baseline-session-id", BASELINE_ATTEMPT_ID,
        ]
        stdout, stderr = _run(command, cwd=tooling_root,
                              label="Flat Root Candidate builder")
        try:
            result = json.loads(stdout)
        except (ValueError, TypeError) as exc:
            raise ValueError("Flat Root builder did not emit JSON: {} {}".format(
                stdout[-1000:], stderr[-1000:]
            )) from exc
        if not isinstance(result, dict) or result.get("status") != "PASSED":
            raise ValueError("Flat Root builder result is not PASSED")

        phase = "Candidate identity and trust verification"
        sys.path.insert(0, tooling_root)
        from app.candidate_artifact import (  # pylint: disable=import-outside-toplevel
            CandidateArtifactManifest, PRODUCTION_PROJECT_NAME,
            PRODUCTION_RELEASE_ARTIFACT_ROLE,
            verify_offline_operator_trust,
        )
        from app.release_publication import (  # pylint: disable=import-outside-toplevel
            validate_production_application_bundle,
            validate_production_candidate_content,
        )
        identity = _load_json(identity_output, "Candidate release identity")
        manifest_path = os.path.join(staging_root, "candidate_artifact_manifest.json")
        manifest = CandidateArtifactManifest.verify(
            staging_root, identity, candidate_sha=SOURCE_SHA,
            manifest_path=manifest_path, require_trusted_provenance=True,
            expected_artifact_role=PRODUCTION_RELEASE_ARTIFACT_ROLE,
            expected_project_name=PRODUCTION_PROJECT_NAME,
            require_production_publishable=True,
        )
        validate_production_candidate_content(staging_root, EXPECTED_PROJECT)
        validate_production_application_bundle(staging_root)
        provenance = manifest.get("source_provenance") or {}
        if manifest.get("artifact_role") != "production_candidate" or \
                manifest.get("production_publishable") is not True or \
                manifest.get("project_name") != EXPECTED_PROJECT or \
                manifest.get("source_commit_sha") != SOURCE_SHA or \
                manifest.get("source_tree_sha") != SOURCE_TREE_SHA or \
                provenance.get("flat_source_binding") != baseline["flat_source_binding"]:
            raise ValueError("Candidate identity does not match pinned inputs")
        if provenance.get("previous_release_commit_sha") != BASELINE_SHA or \
                provenance.get("served_root_tree_sha256") != BASELINE_TREE_SHA256 or \
                provenance.get("served_root_identity_sha256") != \
                _canonical(baseline["identity"]):
            raise ValueError("Candidate legacy baseline binding is invalid")
        verify_offline_operator_trust(
            staging_root, identity, manifest, source_root,
            evidence_path=offline_path, source_bundle_path=source_bundle,
            expected_repository=EXPECTED_REPOSITORY,
            expected_production_host=EXPECTED_HOST,
            expected_production_baseline_sha=BASELINE_SHA,
            expected_validation_session_id=session_id,
        )
        flat_binding_module = __import__(
            "scripts.release.prepare_legacy_flat_adoption",
            fromlist=["verify_flat_source_binding"],
        )
        flat_binding_module.verify_flat_source_binding(
            baseline["flat_root"], baseline["identity_path"], BASELINE_SHA,
            baseline["flat_source_binding"],
        )
        result["candidate_artifact_manifest"] = manifest_path
        result["candidate_build_attestation"] = os.path.join(
            staging_root, "candidate_build_attestation.json"
        )
        result["release_identity"] = identity_output
        result["offline_operator_evidence"] = offline_path
        result["candidate_root_staging"] = staging_root
        result["source_bundle_sha256"] = _sha256(source_bundle)
        result["source_binding_verified_after_build"] = True

        phase = "atomic isolated Candidate finalization"
        if os.path.lexists(candidate_root):
            raise ValueError("final Candidate destination already exists")
        os.replace(staging_root, candidate_root)
        staging_root = ""
        manifest_path = os.path.join(candidate_root, "candidate_artifact_manifest.json")
        attestation_path = os.path.join(candidate_root, "candidate_build_attestation.json")
        manifest = CandidateArtifactManifest.verify(
            candidate_root, identity, candidate_sha=SOURCE_SHA,
            manifest_path=manifest_path, require_trusted_provenance=True,
            expected_artifact_role=PRODUCTION_RELEASE_ARTIFACT_ROLE,
            expected_project_name=PRODUCTION_PROJECT_NAME,
            require_production_publishable=True,
        )
        verify_offline_operator_trust(
            candidate_root, identity, manifest, source_root,
            evidence_path=offline_path, source_bundle_path=source_bundle,
            expected_repository=EXPECTED_REPOSITORY,
            expected_production_host=EXPECTED_HOST,
            expected_production_baseline_sha=BASELINE_SHA,
            expected_validation_session_id=session_id,
        )
        phase = "compact evidence and completion marker"
        result["candidate_root"] = candidate_root
        result["artifact_sha256"] = manifest["artifact_sha256"]
        result["status"] = "PASSED"
        result["verification"] = {
            "candidate_manifest": "PASSED",
            "candidate_attestation": "PASSED",
            "offline_operator_trust": "PASSED",
            "flat_source_binding": "PASSED",
            "application_bundle": "PASSED",
        }
        _write_compact_evidence(
            state_root, candidate_root, identity_output, manifest_path,
            attestation_path, offline_path, baseline["flat_source_binding"],
            result, args.operator_sha256, session_id, source_bundle,
            args.tooling_bundle, space,
        )
        _write_bytes(os.path.join(state_root, "CANDIDATE_COMPLETE"), EXPECTED_MARKER_BYTES)
        _write_json(os.path.join(state_root, "state.json"), {
            "schema_version": 1,
            "status": "PASSED",
            "validation_session_id": session_id,
            "source_sha": SOURCE_SHA,
            "source_tree_sha": SOURCE_TREE_SHA,
            "baseline_attempt_id": BASELINE_ATTEMPT_ID,
            "candidate_root": candidate_root,
            "candidate_artifact_sha256": manifest["artifact_sha256"],
            "compact_evidence": os.path.join(
                state_root, "r8-flat-candidate-evidence.zip"
            ),
        })
        print("CANDIDATE_BUILD=PASS")
        print("CANDIDATE_ROOT={}".format(candidate_root))
        print("CANDIDATE_ARTIFACT_SHA256={}".format(manifest["artifact_sha256"]))
        print("COMPACT_EVIDENCE={}".format(
            os.path.join(state_root, "r8-flat-candidate-evidence.zip")
        ))
        print("PRODUCTION_MUTATION=NONE")
        return 0
    except BaseException as exc:
        _cleanup_partial(state_root, staging_root, candidate_root)
        _record_failure(state_root, session_id, phase, exc)
        print("CANDIDATE_BUILD=BLOCKED", file=sys.stderr)
        print("BLOCKER={}".format(exc), file=sys.stderr)
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="r8-flat-production-candidate-operator-runtime"
    )
    parser.add_argument("--mode", choices=("self-test", "build"), required=True)
    parser.add_argument("--source-bundle", required=True)
    parser.add_argument("--tooling-bundle", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--tooling-root", required=True)
    parser.add_argument("--source-bundle-sha256", required=True)
    parser.add_argument("--tooling-bundle-sha256", required=True)
    parser.add_argument("--state-root", default="")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--operator-sha256", default="")
    args = parser.parse_args(argv)
    if args.mode == "self-test":
        return self_test(
            args.source_bundle, args.tooling_bundle, args.source_root,
            args.tooling_root, args.source_bundle_sha256,
            args.tooling_bundle_sha256,
        )
    if not args.state_root or not args.session_id or not args.operator_sha256:
        parser.error("build mode requires state/session/operator identity")
    return build(args)


if __name__ == "__main__":
    sys.exit(main())
