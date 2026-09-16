#!/usr/bin/env python3
"""Generic offline production-candidate operator runtime.

The runtime is embedded in a standalone operator TXT. It verifies immutable
inputs, reconstructs exact source and tooling checkouts from local Git
bundles, and delegates Candidate construction to the pinned local builder.
It never publishes a release, changes CURRENT, touches Nginx or systemd,
changes a database, or contacts a network service.

This file is Python 3.6-compatible and self-contained because a future
operator executes its embedded copy before a repository checkout exists on
VFOS.
"""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile


SCHEMA_VERSION = 1
OFFLINE_OPERATOR = "offline_operator"
SHA40 = re.compile(r"^[0-9a-fA-F]{40}$")
SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SAFE_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REQUIRED_FIELDS = (
    "schema_version", "repository", "source_commit_sha", "source_tree_sha",
    "source_bundle_sha256", "tooling_commit_sha", "tooling_tree_sha",
    "tooling_bundle_sha256", "production_host", "baseline_commit_sha",
    "baseline_attempt_id", "baseline_tree_sha256", "baseline_manifest_sha256",
    "baseline_adoption_identity_sha256", "project_name", "candidate_parent",
    "release_trust_mode",
)
SHA40_FIELDS = (
    "source_commit_sha", "source_tree_sha", "tooling_commit_sha",
    "tooling_tree_sha", "baseline_commit_sha",
)
SHA256_FIELDS = (
    "source_bundle_sha256", "tooling_bundle_sha256", "baseline_tree_sha256",
    "baseline_manifest_sha256", "baseline_adoption_identity_sha256",
)
PATH_FIELDS = (
    "candidate_parent", "baseline_root", "baseline_manifest_path",
    "baseline_adoption_identity_path", "flat_served_root",
    "flat_release_identity_path", "served_root", "application_root",
    "publish_root", "production_publish_root",
)


def _real(path):
    return os.path.realpath(os.path.abspath(str(path)))


def _inside(root, path):
    try:
        return os.path.commonpath((_real(root), _real(path))) == _real(root)
    except (AttributeError, ValueError):
        return False


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _exact(value, pattern, label):
    value = str(value or "").strip().lower()
    if not pattern.fullmatch(value) or value == "0" * len(value):
        raise ValueError("{} must be a concrete exact SHA".format(label))
    return value


def _text(value, label):
    value = str(value or "").strip()
    if not value or "\x00" in value:
        raise ValueError("{} is invalid".format(label))
    return value


def _path(value, label, required=False):
    if value in (None, "") and not required:
        return ""
    value = _text(value, label)
    if not os.path.isabs(value):
        raise ValueError("{} must be absolute".format(label))
    if ".." in value.replace("\\", "/").split("/"):
        raise ValueError("{} contains path traversal".format(label))
    return value


def validate_manifest(value):
    """Validate the immutable embedded release-input manifest."""
    if not isinstance(value, dict):
        raise ValueError("release input manifest must be a JSON object")
    missing = [field for field in REQUIRED_FIELDS if field not in value]
    if missing:
        raise ValueError("release input manifest is missing {}".format(
            ",".join(missing)
        ))
    manifest = dict(value)
    if isinstance(manifest.get("schema_version"), bool) or \
            manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("release input manifest schema_version must be 1")
    manifest["repository"] = _text(manifest.get("repository"), "repository")
    if not SAFE_REPOSITORY.fullmatch(manifest["repository"]):
        raise ValueError("repository is not canonical")
    for field in SHA40_FIELDS:
        manifest[field] = _exact(manifest.get(field), SHA40, field)
    for field in SHA256_FIELDS:
        manifest[field] = _exact(manifest.get(field), SHA256, field)
    for field in PATH_FIELDS:
        manifest[field] = _path(
            manifest.get(field), field, required=(field == "candidate_parent")
        )
    manifest["production_host"] = _text(
        manifest.get("production_host"), "production_host"
    )
    if not SAFE_ID.fullmatch(manifest["production_host"]):
        raise ValueError("production_host contains unsafe characters")
    manifest["baseline_attempt_id"] = _text(
        manifest.get("baseline_attempt_id"), "baseline_attempt_id"
    )
    if not SAFE_ID.fullmatch(manifest["baseline_attempt_id"]):
        raise ValueError("baseline_attempt_id contains unsafe characters")
    manifest["project_name"] = _text(manifest.get("project_name"), "project_name")
    if not SAFE_ID.fullmatch(manifest["project_name"]):
        raise ValueError("project_name contains unsafe characters")
    if manifest.get("release_trust_mode") != OFFLINE_OPERATOR:
        raise ValueError(
            "release_trust_mode must be offline_operator; protected_builder "
            "is legacy verify-only"
        )
    build_mode = manifest.get("candidate_build_mode", "production")
    if build_mode not in ("production", "flat_production", "validation_fixture"):
        raise ValueError("candidate_build_mode is unsupported")
    manifest["candidate_build_mode"] = build_mode
    if "production_publishable" in manifest and \
            not isinstance(manifest["production_publishable"], bool):
        raise ValueError("production_publishable must be boolean")
    if manifest.get("production_publishable") is True:
        raise ValueError("production_publishable must be false before VFOS validation")
    if build_mode == "validation_fixture" and \
            manifest.get("production_publishable", False) is not False:
        raise ValueError("validation fixture production_publishable must be false")
    if manifest.get("validation_session_id") not in (None, ""):
        manifest["validation_session_id"] = _text(
            manifest["validation_session_id"], "validation_session_id"
        )
        if not SAFE_ID.fullmatch(manifest["validation_session_id"]):
            raise ValueError("validation_session_id contains unsafe characters")
    for field in ("operator_sha256", "release_manifest_sha256"):
        if manifest.get(field) not in (None, ""):
            manifest[field] = _exact(manifest[field], SHA256, field)
    if manifest["candidate_parent"] == "/":
        raise ValueError("candidate_parent is too broad")
    return manifest


def load_manifest(path, expected_sha256):
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError("release input manifest is missing or linked")
    with open(path, "r", encoding="utf-8") as stream:
        value = json.load(stream)
    manifest = validate_manifest(value)
    observed = hashlib.sha256(_canonical_bytes(manifest)).hexdigest()
    expected = _exact(expected_sha256, SHA256, "release input manifest SHA256")
    if observed != expected:
        raise ValueError("release input manifest SHA256 mismatch")
    return manifest


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
    return output.strip()


def _regular(path, label):
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError("{} must be a regular file".format(label))


def _directory(path, label):
    path = os.path.abspath(str(path))
    if os.path.islink(path) or not os.path.isdir(path) or _real(path) != path:
        raise ValueError("{} must be a real directory".format(label))
    return path


def verify_bundle(path, expected_sha256, expected_commit, expected_tree,
                  checkout, label):
    """Verify bytes and reconstruct one exact local Git checkout."""
    path = os.path.abspath(str(path))
    _regular(path, label + " bundle")
    expected_sha256 = _exact(expected_sha256, SHA256, label + " bundle SHA256")
    expected_commit = _exact(expected_commit, SHA40, label + " commit")
    expected_tree = _exact(expected_tree, SHA40, label + " tree")
    if _sha256(path) != expected_sha256:
        raise ValueError("{} bundle SHA256 mismatch".format(label))
    checkout = os.path.abspath(str(checkout))
    if os.path.lexists(checkout):
        raise ValueError("{} checkout destination already exists".format(label))
    parent = os.path.dirname(checkout)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    verifier = tempfile.mkdtemp(prefix="offline-bundle-verify-")
    try:
        _run(("git", "init", "-q"), cwd=verifier, label=label + " verifier init")
        _run(("git", "bundle", "verify", path), cwd=verifier,
             label=label + " bundle verify")
        _run(("git", "clone", "-q", path, checkout),
             label=label + " bundle clone")
        _run(("git", "checkout", "--detach", "-q", expected_commit),
             cwd=checkout, label=label + " exact checkout")
        observed_commit = _run(("git", "rev-parse", "HEAD"), cwd=checkout,
                               label=label + " HEAD").lower()
        observed_tree = _run(("git", "rev-parse", "HEAD^{tree}"), cwd=checkout,
                             label=label + " tree").lower()
        if observed_commit != expected_commit or observed_tree != expected_tree:
            raise ValueError("{} checkout identity mismatch".format(label))
        if _run(("git", "status", "--porcelain", "--untracked-files=all"),
                cwd=checkout, label=label + " checkout status"):
            raise ValueError("{} checkout is dirty".format(label))
        _run(("git", "fsck", "--full", "--strict"), cwd=checkout,
             label=label + " bundle fsck")
    finally:
        shutil.rmtree(verifier, ignore_errors=True)
    return {
        "commit": expected_commit,
        "tree": expected_tree,
        "sha256": expected_sha256,
        "checkout": checkout,
    }


def _verify_tool_contract(tooling_root):
    required = (
        "app/release_trust_policy.py",
        "scripts/release/build_production_candidate_artifact.py",
        "scripts/release/build_flat_production_candidate_artifact.py",
    )
    for relative in required:
        path = os.path.join(tooling_root, *relative.split("/"))
        _regular(path, "tooling contract {}".format(relative))
        with open(path, "rb") as stream:
            data = stream.read()
        if b"offline_operator" not in data:
            raise ValueError("tooling contract does not declare offline_operator: {}".format(
                relative
            ))
    policy_path = os.path.join(tooling_root, "app", "release_trust_policy.py")
    with open(policy_path, "rb") as stream:
        policy = stream.read()
    if b"CURRENT_PRODUCTION_RELEASE_TRUST_MODE" not in policy or \
            b"CURRENT_PRODUCTION_RELEASE_TRUST_MODES" not in policy:
        raise ValueError("tooling trust policy is not canonical")


def _verify_baseline(manifest):
    """Read-only baseline verification driven entirely by manifest paths."""
    root = manifest.get("baseline_root")
    manifest_path = manifest.get("baseline_manifest_path")
    adoption_path = manifest.get("baseline_adoption_identity_path")
    if any(not value for value in (root, manifest_path, adoption_path)):
        raise ValueError(
            "production Candidate build requires baseline_root, "
            "baseline_manifest_path, and baseline_adoption_identity_path"
        )
    root = _directory(root, "baseline root")
    for path, label in (
            (manifest_path, "baseline manifest"),
            (adoption_path, "baseline adoption identity")):
        path = os.path.abspath(path)
        if not _inside(root, path):
            raise ValueError("{} escapes baseline root".format(label))
        _regular(path, label)
    if _sha256(manifest_path) != manifest["baseline_manifest_sha256"]:
        raise ValueError("baseline manifest SHA256 mismatch")
    if _sha256(adoption_path) != manifest["baseline_adoption_identity_sha256"]:
        raise ValueError("baseline adoption identity SHA256 mismatch")
    try:
        with open(manifest_path, "r", encoding="utf-8") as stream:
            baseline_manifest = json.load(stream)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("baseline manifest is unreadable: {}".format(exc))
    if not isinstance(baseline_manifest, dict):
        raise ValueError("baseline manifest must be an object")
    declared_commit = (
        baseline_manifest.get("expected_baseline_sha") or
        baseline_manifest.get("baseline_commit_sha") or
        baseline_manifest.get("commit_sha")
    )
    if declared_commit and str(declared_commit).lower() != manifest["baseline_commit_sha"]:
        raise ValueError("baseline commit SHA mismatch")
    declared_tree = (
        baseline_manifest.get("source_canonical_tree_sha256") or
        baseline_manifest.get("baseline_tree_sha256") or
        baseline_manifest.get("tree_sha256")
    )
    if not declared_tree or str(declared_tree).lower() != manifest["baseline_tree_sha256"]:
        raise ValueError("baseline tree SHA256 mismatch")
    return {
        "root": root,
        "manifest": os.path.abspath(manifest_path),
        "adoption_identity": os.path.abspath(adoption_path),
    }


def _assert_preparation_only(manifest):
    candidate_parent = _real(manifest["candidate_parent"])
    publish_root = manifest.get("production_publish_root")
    if publish_root:
        publish_root = _real(publish_root)
        if _inside(publish_root, candidate_parent) or _inside(candidate_parent, publish_root):
            raise ValueError("candidate parent overlaps production publish root")
    for field in ("current_path", "nginx_config_path", "systemd_unit"):
        if manifest.get(field):
            raise ValueError("preparation-only operator may not carry {}".format(field))


def _builder_command(manifest, source_root, candidate_root, identity_output,
                    evidence_output, source_bundle, session_id, tooling_root,
                    release_input_manifest_path,
                    release_input_manifest_sha256):
    mode = manifest["candidate_build_mode"]
    builder = os.path.join(
        tooling_root, "scripts", "release",
        "build_flat_production_candidate_artifact.py"
        if mode == "flat_production" else
        "build_production_candidate_artifact.py",
    )
    command = [
        sys.executable, builder,
        "--source-repo-root", source_root,
        "--production-candidate-root", candidate_root,
        "--release-identity-output", identity_output,
        "--release-trust-mode", OFFLINE_OPERATOR,
        "--offline-operator-evidence-output", evidence_output,
        "--offline-operator-source-bundle", source_bundle,
        "--offline-operator-repository", manifest["repository"],
        "--production-host", manifest["production_host"],
        "--production-baseline-sha", manifest["baseline_commit_sha"],
        "--validation-session-id", session_id,
        "--release-input-manifest", release_input_manifest_path,
        "--release-input-manifest-sha256", release_input_manifest_sha256,
    ]
    if mode == "flat_production":
        required = ("flat_served_root", "flat_release_identity_path")
        if any(not manifest.get(field) for field in required):
            raise ValueError("flat production manifest is missing Flat Root inputs")
        command.extend([
            "--flat-served-root", manifest["flat_served_root"],
            "--flat-release-identity", manifest["flat_release_identity_path"],
            "--expected-previous-release-sha", manifest["baseline_commit_sha"],
            "--flat-baseline-session-id", manifest["baseline_attempt_id"],
        ])
    else:
        required = (
            "served_root", "served_root_tree_sha256",
            "served_root_identity_sha256",
        )
        if any(not manifest.get(field) for field in required):
            raise ValueError("production manifest is missing Served Root inputs")
        command.extend([
            "--served-root", manifest["served_root"],
            "--expected-previous-release-sha", manifest["baseline_commit_sha"],
            "--expected-served-root-tree-sha256", manifest["served_root_tree_sha256"],
            "--expected-current-identity-sha256",
            manifest["served_root_identity_sha256"],
        ])
    return command


def self_test(manifest_path, manifest_sha256, source_bundle, tooling_bundle,
              source_root, tooling_root, source_bundle_sha256,
              tooling_bundle_sha256):
    manifest = load_manifest(manifest_path, manifest_sha256)
    _assert_preparation_only(manifest)
    source_root = os.path.abspath(source_root)
    tooling_root = os.path.abspath(tooling_root)
    if os.path.lexists(source_root) or os.path.lexists(tooling_root):
        raise ValueError("self-test checkout destination already exists")
    with tempfile.TemporaryDirectory(prefix="offline-operator-self-test-") as root:
        source = os.path.join(root, "source")
        tooling = os.path.join(root, "tooling")
        verify_bundle(
            source_bundle, source_bundle_sha256,
            manifest["source_commit_sha"], manifest["source_tree_sha"],
            source, "application source"
        )
        verify_bundle(
            tooling_bundle, tooling_bundle_sha256,
            manifest["tooling_commit_sha"], manifest["tooling_tree_sha"],
            tooling, "release tooling"
        )
        _verify_tool_contract(tooling)
    print("RELEASE_INPUT_MANIFEST_SHA256={}".format(manifest_sha256.lower()))
    print("SOURCE_IDENTITY=PASS")
    print("TOOLING_IDENTITY=PASS")
    print("PRODUCTION_MUTATION=NONE")
    print("OPERATOR_SELF_TEST=PASS")
    return 0


def _directory_or_create(path, label):
    path = os.path.abspath(str(path))
    if os.path.lexists(path):
        return _directory(path, label)
    parent = os.path.dirname(path)
    if parent and os.path.islink(parent):
        raise ValueError("{} parent may not be a symlink".format(label))
    os.makedirs(path)
    return _directory(path, label)


def build(args):
    manifest = load_manifest(
        args.release_input_manifest, args.release_input_manifest_sha256
    )
    if manifest["candidate_build_mode"] == "validation_fixture":
        raise ValueError("VALIDATION_FIXTURE_PRODUCTION_PUBLISHABLE=NO")
    if manifest.get("validation_session_id") and \
            manifest["validation_session_id"] != args.session_id:
        raise ValueError("validation session ID does not match manifest")
    if not SAFE_ID.fullmatch(str(args.session_id or "")):
        raise ValueError("session ID is unsafe")
    operator_sha = _exact(args.operator_sha256, SHA256, "operator SHA256")
    _assert_preparation_only(manifest)
    if socket.gethostname().split(".", 1)[0] != manifest["production_host"]:
        raise ValueError("operator host does not match manifest production_host")
    _verify_baseline(manifest)
    candidate_parent = _directory_or_create(
        manifest["candidate_parent"], "candidate parent"
    )
    state_root = os.path.abspath(args.state_root)
    if not _inside(candidate_parent, state_root) or state_root == candidate_parent:
        raise ValueError("state root must be a child of candidate_parent")
    if os.path.lexists(state_root) or os.path.islink(state_root):
        raise ValueError("candidate state root already exists")
    os.makedirs(state_root)
    source_root = os.path.join(state_root, "source")
    tooling_root = os.path.join(state_root, "tooling")
    input_root = os.path.join(state_root, "input")
    os.makedirs(input_root)
    source_bundle = os.path.join(input_root, "source.bundle")
    tooling_bundle = os.path.join(input_root, "tooling.bundle")
    shutil.copyfile(args.source_bundle, source_bundle)
    shutil.copyfile(args.tooling_bundle, tooling_bundle)
    try:
        verify_bundle(
            source_bundle, manifest["source_bundle_sha256"],
            manifest["source_commit_sha"], manifest["source_tree_sha"],
            source_root, "application source"
        )
        verify_bundle(
            tooling_bundle, manifest["tooling_bundle_sha256"],
            manifest["tooling_commit_sha"], manifest["tooling_tree_sha"],
            tooling_root, "release tooling"
        )
        _verify_tool_contract(tooling_root)
        identity_output = os.path.join(state_root, "release_identity.json")
        evidence_output = os.path.join(state_root, "offline_operator_evidence.json")
        command = _builder_command(
            manifest, source_root, os.path.join(state_root, "candidate"),
            identity_output, evidence_output, source_bundle, args.session_id,
            tooling_root, args.release_input_manifest,
            args.release_input_manifest_sha256,
        )
        _run(command, cwd=tooling_root, label="offline Candidate builder")
        output_manifest = os.path.join(state_root, "release_input_manifest.json")
        with open(output_manifest, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        with open(os.path.join(state_root, "release_input_manifest.sha256"),
                  "w", encoding="ascii") as stream:
            stream.write("{}  release_input_manifest.json\n".format(
                args.release_input_manifest_sha256.lower()
            ))
        with open(os.path.join(state_root, "operator_attempt.json"),
                  "w", encoding="utf-8") as stream:
            json.dump({
                "schema_version": 1,
                "release_input_manifest_sha256": args.release_input_manifest_sha256.lower(),
                "operator_sha256": operator_sha,
                "session_id": args.session_id,
                "source_commit_sha": manifest["source_commit_sha"],
                "source_tree_sha": manifest["source_tree_sha"],
                "baseline_commit_sha": manifest["baseline_commit_sha"],
                "release_trust_mode": OFFLINE_OPERATOR,
                "production_mutation": "NONE",
            }, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        print("CANDIDATE_BUILD=PASS")
        print("RELEASE_INPUT_MANIFEST_SHA256={}".format(
            args.release_input_manifest_sha256.lower()
        ))
        print("CANDIDATE_ROOT={}".format(os.path.join(state_root, "candidate")))
        print("PRODUCTION_MUTATION=NONE")
        return 0
    except BaseException as exc:
        with open(os.path.join(state_root, "candidate_build_failure.json"),
                  "w", encoding="utf-8") as stream:
            json.dump({
                "schema_version": 1,
                "status": "BLOCKED",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "release_input_manifest_sha256": args.release_input_manifest_sha256,
                "operator_sha256": operator_sha,
                "production_mutation": "NONE",
            }, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        print("CANDIDATE_BUILD=BLOCKED", file=sys.stderr)
        print("BLOCKER={}".format(exc), file=sys.stderr)
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="flat_candidate_operator_runtime.py")
    parser.add_argument("--mode", choices=("self-test", "build"), required=True)
    parser.add_argument("--release-input-manifest", required=True)
    parser.add_argument("--release-input-manifest-sha256", required=True)
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
    try:
        if args.mode == "self-test":
            return self_test(
                args.release_input_manifest,
                args.release_input_manifest_sha256,
                args.source_bundle, args.tooling_bundle,
                args.source_root, args.tooling_root,
                args.source_bundle_sha256, args.tooling_bundle_sha256,
            )
        if not args.state_root or not args.session_id or not args.operator_sha256:
            parser.error("build mode requires state/session/operator identity")
        return build(args)
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print("OPERATOR_SELF_TEST=FAIL", file=sys.stderr)
        print("BLOCKER={}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
