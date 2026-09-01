"""Explicitly adopt an existing Served Root as the immutable baseline.

This is a one-time operator action for environments that predate immutable
publication.  Normal upgrades deliberately do not call this module when
``CURRENT`` is absent.
"""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.candidate_artifact import CandidateArtifactManifest, identity_manifest_sha256
from app.release_publication import (
    ImmutableReleasePublisher, normalize_candidate_artifact,
    validate_release_manifest,
)


IDENTITY_KEYS = (
    "version", "commit_sha", "build_id", "asset_hash", "schema_version",
    "asset_manifest_version", "asset_count", "asset_manifest_hash",
    "asset_manifest",
)


def _load_json(path, description):
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError) as exc:
        raise ValueError("{} is unreadable: {}".format(description, exc))
    if not isinstance(value, dict):
        raise ValueError("{} must be a JSON object".format(description))
    return value


def _identity_from_payload(payload):
    nested = payload.get("release_identity")
    return nested if isinstance(nested, dict) else payload


def _verify_identity(expected, observed):
    actual = _identity_from_payload(observed)
    mismatches = []
    for key in IDENTITY_KEYS:
        if expected.get(key) in (None, ""):
            mismatches.append("expected identity is missing {}".format(key))
        elif actual.get(key) != expected.get(key):
            mismatches.append("served identity mismatch: {}".format(key))
    if mismatches:
        raise ValueError("; ".join(mismatches))
    return actual


def _find_served_identity(served_root, explicit_path):
    candidates = []
    if explicit_path:
        candidates.append(os.path.abspath(explicit_path))
    candidates.extend([
        os.path.join(served_root, "release_identity.json"),
        os.path.join(served_root, "release_manifest.json"),
    ])
    seen = set()
    for path in candidates:
        path = os.path.realpath(path)
        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        return path, _load_json(path, "served root identity")
    raise ValueError(
        "current Served Root identity evidence is required; provide --served-identity"
    )


def _copy_source_without_following_links(source_root, target_root):
    """Copy the legacy root to a disposable build tree without mutating it."""
    if not os.path.isdir(target_root):
        os.makedirs(target_root)
    for name in sorted(os.listdir(source_root)):
        source = os.path.join(source_root, name)
        target = os.path.join(target_root, name)
        if os.path.isdir(source) and not os.path.islink(source):
            shutil.copytree(source, target, symlinks=True)
        elif os.path.isfile(source) or os.path.islink(source):
            shutil.copy2(source, target, follow_symlinks=False)
        else:
            raise ValueError("unsupported Served Root entry: {}".format(source))


def _served_root_tree_sha(root):
    """Create a deterministic tree identity for a non-Git Served Root."""
    entries = []
    root = os.path.realpath(os.path.abspath(root))
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(dirnames)
        for name in sorted(filenames):
            path = os.path.join(directory, name)
            if os.path.islink(path) or not os.path.isfile(path):
                raise ValueError("served root contains an unsupported link or file: {}".format(path))
            digest = hashlib.sha256()
            with open(path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            entries.append({
                "path": os.path.relpath(path, root).replace(os.sep, "/"),
                "size": int(os.path.getsize(path)),
                "sha256": digest.hexdigest(),
            })
    canonical = json.dumps(
        sorted(entries, key=lambda item: item["path"]),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha1(canonical).hexdigest()


def bootstrap(served_root, publish_root, release_identity_path, session_id,
              served_identity_path="", switch=False, api_contract_version=""):
    served_root = os.path.realpath(os.path.abspath(served_root))
    publish_root = os.path.realpath(os.path.abspath(publish_root))
    if not os.path.isdir(served_root):
        raise ValueError("served root is not a directory: {}".format(served_root))
    if os.path.commonpath((served_root, publish_root)) == served_root:
        raise ValueError("publish root must be separate from Served Root")
    expected = _load_json(release_identity_path, "release identity")
    identity_path, observed_payload = _find_served_identity(
        served_root, served_identity_path
    )
    observed = _verify_identity(expected, observed_payload)
    # If the current root already carries the immutable publication manifest,
    # validate its physical Served Root before using it as the baseline.  A
    # legacy root may instead expose the identity in a standalone JSON file.
    if isinstance(observed_payload, dict) and \
            observed_payload.get("release_validation_session_id"):
        current_check = validate_release_manifest(served_root, observed_payload)
        if current_check.get("status") != "PASSED":
            raise ValueError(
                "current Served Root immutable manifest is invalid: {}".format(
                    "; ".join(current_check.get("violations") or [])
                )
            )

    publisher = ImmutableReleasePublisher(publish_root)
    if os.path.lexists(publisher.current_path):
        raise ValueError(
            "immutable CURRENT already exists; bootstrap is one-time only"
        )
    if not switch:
        raise ValueError(
            "bootstrap is explicit and must be invoked with --switch to create CURRENT"
        )

    with tempfile.TemporaryDirectory(prefix="coverage-bootstrap-") as build_root:
        _copy_source_without_following_links(served_root, build_root)
        # Bootstrap creates the Candidate bytes that are about to be hashed;
        # the Publisher must not normalize a different copy later.
        normalize_candidate_artifact(build_root)
        artifact_manifest = CandidateArtifactManifest.build(
            build_root, expected,
            source_provenance={
                "provenance_class": "served-root-bootstrap",
                "source_commit_sha": expected.get("commit_sha"),
                "source_tree_sha": _served_root_tree_sha(served_root),
                "worktree_clean": True,
                "build_workflow_identity": "bootstrap_previous_release",
                "source_manifest_sha256": identity_manifest_sha256(expected),
            },
        )
        prepared = publisher.prepare(
            build_root, expected, session_id,
            api_contract_version=api_contract_version,
        )
    switched = publisher.switch_current(session_id)
    checked = publisher.validate_current()
    if checked.get("status") != "PASSED":
        raise RuntimeError(
            "bootstrapped CURRENT failed validation: {}".format(
                "; ".join(checked.get("violations") or [])
            )
        )
    if checked.get("commit_sha") != expected.get("commit_sha"):
        raise RuntimeError("bootstrapped CURRENT commit does not match identity")
    return {
        "status": "PASSED",
        "evidence_class": "immutable_previous_release_bootstrap",
        "served_root": served_root,
        "served_identity_path": identity_path,
        "served_release_identity": observed,
        "release_validation_session_id": session_id,
        "release_root": publisher.release_path(session_id),
        "candidate_artifact_manifest": {
            "artifact_manifest_version": artifact_manifest.get("artifact_manifest_version"),
            "artifact_sha256": artifact_manifest.get("artifact_sha256"),
            "reports_sha256": artifact_manifest.get("reports_sha256"),
            "assets_sha256": artifact_manifest.get("assets_sha256"),
            "registry_sha256": artifact_manifest.get("registry_sha256"),
            "file_count": len(artifact_manifest.get("files") or []),
        },
        "release_manifest": prepared,
        "switch": switched,
        "validation": checked,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="bootstrap_previous_release.py")
    parser.add_argument("--served-root", required=True)
    parser.add_argument("--publish-root", required=True)
    parser.add_argument("--release-identity", required=True)
    parser.add_argument(
        "--served-identity", default="",
        help="identity JSON captured from the actual Served Root",
    )
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--api-contract-version", default="")
    parser.add_argument("--switch", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = bootstrap(
            args.served_root, args.publish_root, args.release_identity,
            args.session_id, served_identity_path=args.served_identity,
            switch=args.switch, api_contract_version=args.api_contract_version,
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
