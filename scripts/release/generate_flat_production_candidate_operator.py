#!/usr/bin/env python3
"""Generate a standalone, manifest-bound offline Candidate operator.

The output is a preparation-only TXT file.  All source, tooling, baseline,
host, and project values are read from one immutable release-input manifest;
the generator contains no release-version constants.  The generated operator
embeds the manifest, both Git bundles, and the generic runtime, then verifies
every byte before a future VFOS-side Candidate build.
"""

from __future__ import print_function

import argparse
import base64
import hashlib
import os
import shlex
import subprocess
import tempfile

from scripts.release.release_input_manifest import (
    canonical_json_bytes,
    load_release_input_manifest,
    validate_release_input_manifest,
    release_input_manifest_sha256,
)


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
RUNTIME_PATH = os.path.join(
    ROOT, "scripts", "release", "flat_candidate_operator_runtime.py"
)
SHA40 = set("0123456789abcdef")


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    path = os.path.abspath(str(path))
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError("{} must be a regular file".format(label))
    return path


def _exact_sha(value, label, length):
    value = str(value or "").strip().lower()
    if len(value) != length or any(char not in SHA40 for char in value) or \
            value == "0" * length:
        raise ValueError("{} must be a concrete exact SHA".format(label))
    return value


def _validate_bundle(path, expected_commit, expected_tree, expected_sha256, label):
    path = _regular(path, label)
    expected_commit = _exact_sha(expected_commit, label + " commit", 40)
    expected_tree = _exact_sha(expected_tree, label + " tree", 40)
    expected_sha256 = _exact_sha(expected_sha256, label + " bundle SHA256", 64)
    observed_sha256 = _sha256(path)
    if observed_sha256 != expected_sha256:
        raise ValueError("{} bundle SHA256 mismatch".format(label))
    with tempfile.TemporaryDirectory(prefix="offline-operator-generator-") as root:
        verifier = os.path.join(root, "verifier")
        checkout = os.path.join(root, "checkout")
        os.makedirs(verifier)
        _run(("git", "init", "-q"), cwd=verifier,
             label=label + " verifier init")
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
            raise ValueError("{} does not contain the pinned revision".format(label))
        if _run(("git", "status", "--porcelain", "--untracked-files=all"),
                cwd=checkout, label=label + " checkout status"):
            raise ValueError("{} checkout is dirty".format(label))
        _run(("git", "fsck", "--full", "--strict"), cwd=checkout,
             label=label + " bundle fsck")
    return {
        "path": path,
        "commit": expected_commit,
        "tree": expected_tree,
        "sha256": expected_sha256,
    }


def _b64(value):
    encoded = base64.b64encode(value).decode("ascii")
    return [encoded[index:index + 120] for index in range(0, len(encoded), 120)]


def _quote(value):
    return shlex.quote(str(value))


def _read_bytes(path, label):
    path = _regular(path, label)
    try:
        with open(path, "rb") as stream:
            return stream.read()
    except OSError as exc:
        raise ValueError("{} is unreadable: {}".format(label, exc))


def _extractor():
    return [
        "extract_payload() {",
        "  local NAME=\"$1\" TARGET=\"$2\" EXPECTED=\"$3\" BEGIN=\"$4\" END=\"$5\"",
        "  if [ -e \"$TARGET\" ] || [ -L \"$TARGET\" ]; then",
        "    echo \"$NAME destination already exists: $TARGET\" >&2",
        "    exit 1",
        "  fi",
        "  local TEMP=\"${TARGET}.tmp.$$\"",
        "  awk -v begin=\"$BEGIN\" -v end=\"$END\" '$0 == begin {inside=1; next} $0 == end {inside=0; exit} inside {print}' \"$0\" | base64 -d > \"$TEMP\"",
        "  if [ ! -f \"$TEMP\" ] || [ -L \"$TEMP\" ]; then",
        "    echo \"$NAME extraction failed\" >&2",
        "    exit 1",
        "  fi",
        "  local OBSERVED",
        "  OBSERVED=$(sha256sum \"$TEMP\" | awk '{print $1}')",
        "  if [ \"$OBSERVED\" != \"$EXPECTED\" ]; then",
        "    echo \"$NAME SHA256 mismatch\" >&2",
        "    exit 1",
        "  fi",
        "  mv \"$TEMP\" \"$TARGET\"",
        "}",
    ]


def _script(manifest, manifest_sha256, source, tooling, runtime):
    runtime_sha256 = hashlib.sha256(runtime).hexdigest()
    source_sha = manifest["source_commit_sha"]
    candidate_parent = manifest["candidate_parent"]
    validation_session = manifest.get("validation_session_id", "")
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "umask 077",
        "",
        "# Offline production Candidate preparation only.",
        "# This operator never publishes, switches CURRENT, changes Nginx/systemd, or touches DB.",
        "RELEASE_INPUT_MANIFEST_SHA256={}".format(_quote(manifest_sha256)),
        "SOURCE_COMMIT_SHA={}".format(_quote(source_sha)),
        "SOURCE_TREE_SHA={}".format(_quote(manifest["source_tree_sha"])),
        "SOURCE_BUNDLE_SHA256={}".format(_quote(source["sha256"])),
        "TOOLING_COMMIT_SHA={}".format(_quote(tooling["commit"])),
        "TOOLING_TREE_SHA={}".format(_quote(tooling["tree"])),
        "TOOLING_BUNDLE_SHA256={}".format(_quote(tooling["sha256"])),
        "RUNTIME_SHA256={}".format(_quote(runtime_sha256)),
        "CANDIDATE_PARENT={}".format(_quote(candidate_parent)),
        "VALIDATION_SESSION_ID={}".format(_quote(validation_session)),
        "PYTHON_BIN=\"${FOS_OPERATOR_PYTHON:-python3}\"",
        "",
        "if [ \"$#\" -gt 1 ]; then echo 'usage: bash $0 [--self-test]' >&2; exit 2; fi",
        "ACTION=build",
        "if [ \"${1:-}\" = '--self-test' ]; then ACTION=self-test; fi",
        "if [ \"${1:-}\" != '' ] && [ \"${1:-}\" != '--self-test' ]; then",
        "  echo 'unknown argument; only --self-test is supported' >&2",
        "  exit 2",
        "fi",
        "",
        "for REQUIRED in sha256sum awk base64 git; do",
        "  command -v \"$REQUIRED\" >/dev/null 2>&1 || { echo \"$REQUIRED is required\" >&2; exit 1; }",
        "done",
        "command -v \"$PYTHON_BIN\" >/dev/null 2>&1 || { echo \"Python executable is unavailable: $PYTHON_BIN\" >&2; exit 1; }",
        "OPERATOR_SHA256=$(sha256sum \"$0\" | awk '{print $1}')",
        "",
    ]
    lines.extend(_extractor())
    lines.extend([
        "",
        "if [ \"$ACTION\" = self-test ]; then",
        "  SELF_TEST_ROOT=$(mktemp -d \"${TMPDIR:-/tmp}/fos-offline-operator-self-test.XXXXXX\")",
        "  trap 'rm -rf -- \"$SELF_TEST_ROOT\"' EXIT",
        "  mkdir -p \"$SELF_TEST_ROOT/input\"",
        "  extract_payload manifest \"$SELF_TEST_ROOT/input/release_input_manifest.json\" \"$RELEASE_INPUT_MANIFEST_SHA256\" __MANIFEST_BEGIN__ __MANIFEST_END__",
        "  extract_payload source-bundle \"$SELF_TEST_ROOT/input/source.bundle\" \"$SOURCE_BUNDLE_SHA256\" __SOURCE_BUNDLE_BEGIN__ __SOURCE_BUNDLE_END__",
        "  extract_payload tooling-bundle \"$SELF_TEST_ROOT/input/tooling.bundle\" \"$TOOLING_BUNDLE_SHA256\" __TOOLING_BUNDLE_BEGIN__ __TOOLING_BUNDLE_END__",
        "  extract_payload runtime \"$SELF_TEST_ROOT/runtime.py\" \"$RUNTIME_SHA256\" __RUNTIME_BEGIN__ __RUNTIME_END__",
        "  \"$PYTHON_BIN\" \"$SELF_TEST_ROOT/runtime.py\" --mode self-test \\",
        "    --release-input-manifest \"$SELF_TEST_ROOT/input/release_input_manifest.json\" \\",
        "    --release-input-manifest-sha256 \"$RELEASE_INPUT_MANIFEST_SHA256\" \\",
        "    --source-bundle \"$SELF_TEST_ROOT/input/source.bundle\" \\",
        "    --tooling-bundle \"$SELF_TEST_ROOT/input/tooling.bundle\" \\",
        "    --source-root \"$SELF_TEST_ROOT/source\" \\",
        "    --tooling-root \"$SELF_TEST_ROOT/tooling\" \\",
        "    --source-bundle-sha256 \"$SOURCE_BUNDLE_SHA256\" \\",
        "    --tooling-bundle-sha256 \"$TOOLING_BUNDLE_SHA256\"",
        "  echo \"OPERATOR_SHA256=$OPERATOR_SHA256\"",
        "  echo 'PRODUCTION_MUTATION=NONE'",
        "  exit 0",
        "fi",
        "",
        "if [ -n \"$VALIDATION_SESSION_ID\" ]; then SESSION_ID=\"$VALIDATION_SESSION_ID\"; else SESSION_ID=\"offline-${SOURCE_COMMIT_SHA:0:12}-$(date -u +%Y%m%dT%H%M%SZ)-$$\"; fi",
        "if [ -L \"$CANDIDATE_PARENT\" ]; then echo 'candidate parent may not be a symlink' >&2; exit 1; fi",
        "if [ ! -d \"$CANDIDATE_PARENT\" ]; then mkdir -p \"$CANDIDATE_PARENT\"; fi",
        "STATE_ROOT=\"$CANDIDATE_PARENT/offline-candidate-${SOURCE_COMMIT_SHA:0:12}-$(date -u +%Y%m%dT%H%M%SZ)-$$\"",
        "if [ -e \"$STATE_ROOT\" ] || [ -L \"$STATE_ROOT\" ]; then echo 'candidate state collision; refusing to continue' >&2; exit 1; fi",
        "mkdir -p \"$STATE_ROOT/input\"",
        "extract_payload manifest \"$STATE_ROOT/input/release_input_manifest.json\" \"$RELEASE_INPUT_MANIFEST_SHA256\" __MANIFEST_BEGIN__ __MANIFEST_END__",
        "extract_payload source-bundle \"$STATE_ROOT/input/source.bundle\" \"$SOURCE_BUNDLE_SHA256\" __SOURCE_BUNDLE_BEGIN__ __SOURCE_BUNDLE_END__",
        "extract_payload tooling-bundle \"$STATE_ROOT/input/tooling.bundle\" \"$TOOLING_BUNDLE_SHA256\" __TOOLING_BUNDLE_BEGIN__ __TOOLING_BUNDLE_END__",
        "extract_payload runtime \"$STATE_ROOT/runtime.py\" \"$RUNTIME_SHA256\" __RUNTIME_BEGIN__ __RUNTIME_END__",
        "\"$PYTHON_BIN\" \"$STATE_ROOT/runtime.py\" --mode build \\",
        "  --release-input-manifest \"$STATE_ROOT/input/release_input_manifest.json\" \\",
        "  --release-input-manifest-sha256 \"$RELEASE_INPUT_MANIFEST_SHA256\" \\",
        "  --source-bundle \"$STATE_ROOT/input/source.bundle\" \\",
        "  --tooling-bundle \"$STATE_ROOT/input/tooling.bundle\" \\",
        "  --source-root \"$STATE_ROOT/source\" \\",
        "  --tooling-root \"$STATE_ROOT/tooling\" \\",
        "  --source-bundle-sha256 \"$SOURCE_BUNDLE_SHA256\" \\",
        "  --tooling-bundle-sha256 \"$TOOLING_BUNDLE_SHA256\" \\",
        "  --state-root \"$STATE_ROOT\" \\",
        "  --session-id \"$SESSION_ID\" \\",
        "  --operator-sha256 \"$OPERATOR_SHA256\"",
        "",
        "exit 0",
        "",
        "__RUNTIME_BEGIN__",
    ])
    lines.extend(_b64(runtime))
    lines.extend(["__RUNTIME_END__", "", "__MANIFEST_BEGIN__"])
    lines.extend(_b64(
        json_bytes(manifest)
    ))
    lines.extend(["__MANIFEST_END__", "", "__SOURCE_BUNDLE_BEGIN__"])
    lines.extend(_b64(_read_bytes(source["path"], "source bundle")))
    lines.extend(["__SOURCE_BUNDLE_END__", "", "__TOOLING_BUNDLE_BEGIN__"])
    lines.extend(_b64(_read_bytes(tooling["path"], "tooling bundle")))
    lines.extend(["__TOOLING_BUNDLE_END__", ""])
    return "\n".join(lines).encode("utf-8")


def json_bytes(manifest):
    return canonical_json_bytes(validate_release_input_manifest(manifest))


def generate(release_input_manifest, source_bundle, tooling_bundle, output):
    """Generate one operator from one immutable release-input manifest."""
    manifest_path = _regular(release_input_manifest, "release input manifest")
    # The file's detached sidecar is not trusted implicitly: its canonical
    # digest is recomputed and embedded as the operator's authority value.
    manifest = load_release_input_manifest(manifest_path)
    manifest_sha256 = release_input_manifest_sha256(manifest)
    source = _validate_bundle(
        source_bundle, manifest["source_commit_sha"], manifest["source_tree_sha"],
        manifest["source_bundle_sha256"], "application source"
    )
    tooling = _validate_bundle(
        tooling_bundle, manifest["tooling_commit_sha"], manifest["tooling_tree_sha"],
        manifest["tooling_bundle_sha256"], "release tooling"
    )
    runtime_path = _regular(RUNTIME_PATH, "operator runtime")
    runtime = _read_bytes(runtime_path, "operator runtime")
    rendered = _script(manifest, manifest_sha256, source, tooling, runtime)
    output = os.path.abspath(str(output))
    if os.path.lexists(output):
        raise ValueError("operator output already exists: {}".format(output))
    parent = os.path.dirname(output)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    temporary = "{}.tmp-{}".format(output, os.getpid())
    with open(temporary, "wb") as stream:
        stream.write(rendered)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, output)
    try:
        os.chmod(output, 0o750)
    except OSError:
        pass
    return {
        "operator_file": output,
        "operator_sha256": _sha256(output),
        "operator_size": os.path.getsize(output),
        "release_input_manifest_sha256": manifest_sha256,
        "source_commit_sha": manifest["source_commit_sha"],
        "source_tree_sha": manifest["source_tree_sha"],
        "source_bundle_sha256": manifest["source_bundle_sha256"],
        "tooling_commit_sha": manifest["tooling_commit_sha"],
        "tooling_tree_sha": manifest["tooling_tree_sha"],
        "tooling_bundle_sha256": manifest["tooling_bundle_sha256"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="generate_flat_production_candidate_operator.py"
    )
    parser.add_argument("--release-input-manifest", required=True)
    parser.add_argument("--source-bundle", required=True)
    parser.add_argument("--tooling-bundle", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = generate(
            args.release_input_manifest, args.source_bundle,
            args.tooling_bundle, args.output,
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    print("OPERATOR_GENERATION=PASS")
    for key in sorted(result):
        print("{}={}".format(key.upper(), result[key]))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
