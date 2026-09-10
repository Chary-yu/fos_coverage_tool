#!/usr/bin/env python3
"""Build the single-file, offline R8 operator handoff.

The generated file is deliberately not part of the Git tree.  It embeds the
exact source bundle, the two exact-revision Chromium source artifacts, and the
small metadata record needed by the Python 3.6 conductor.  Generation itself
is fail-closed: the requested SHA/tree, bundle, revisions, workload, and
independent environments are checked before any TXT is written.
"""

from __future__ import print_function

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.upgrade.performance_evidence import validate_revision_artifact


SHA40 = re.compile(r"^[0-9a-fA-F]{40}$")
def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path, label):
    if not os.path.isfile(path) or os.path.islink(path):
        raise ValueError("{} is missing: {}".format(label, path))
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("{} is unreadable: {}".format(label, exc))
    if not isinstance(value, dict):
        raise ValueError("{} must be a JSON object".format(label))
    return value


def _command(argv, cwd=None, check=True):
    try:
        result = subprocess.Popen(
            list(argv), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, stderr = result.communicate()
    except (OSError, ValueError) as exc:
        raise RuntimeError("command failed to start: {}".format(exc))
    output = stdout.decode("utf-8", "replace").strip()
    error = stderr.decode("utf-8", "replace").strip()
    if check and result.returncode != 0:
        raise RuntimeError(
            "command failed ({}): {}".format(
                result.returncode, error or output
            )
        )
    return result.returncode, output, error


def _git(repo_root, *args):
    return _command(("git",) + tuple(args), cwd=repo_root)[1]


def _exact_sha(value, label, pattern):
    value = str(value or "").strip().lower()
    if not pattern.fullmatch(value) or set(value) == {"0"}:
        raise ValueError("{} must be a concrete exact SHA".format(label))
    return value


def _comparable_environment(value):
    if not isinstance(value, dict) or not value:
        return None
    ephemeral = {
        "ci_run", "run_id", "workflow_run_id", "started_at", "finished_at",
        "timestamp",
    }
    return json.dumps(
        {key: item for key, item in value.items() if key not in ephemeral},
        ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _validate_bundle(bundle_path, final_sha, final_tree, baseline_sha):
    if not os.path.isfile(bundle_path) or os.path.islink(bundle_path):
        raise ValueError("exact Git bundle is missing")
    _command(("git", "bundle", "verify", bundle_path), cwd=ROOT)
    with tempfile.TemporaryDirectory(prefix="fos-r8-bundle-check-") as temporary:
        checkout = os.path.join(temporary, "checkout")
        _command(("git", "clone", bundle_path, checkout), cwd=ROOT)
        _command(("git", "checkout", final_sha), cwd=checkout)
        observed_sha = _git(checkout, "rev-parse", "HEAD").lower()
        observed_tree = _git(checkout, "rev-parse", "HEAD^{tree}").lower()
        if observed_sha != final_sha:
            raise ValueError("bundle clone HEAD does not match FINAL_R8_SHA")
        if observed_tree != final_tree:
            raise ValueError("bundle clone tree does not match FINAL_R8_TREE")
        _command(("git", "cat-file", "-e", baseline_sha + "^{commit}"), cwd=checkout)
        _command(("git", "fsck", "--full", "--strict"), cwd=checkout)
        dirty = _git(checkout, "status", "--porcelain", "--untracked-files=all")
        if dirty:
            raise ValueError("bundle clone is not clean")


def _validate_source_checkout(repo_root, final_sha, final_tree, baseline_sha):
    repo_root = os.path.realpath(os.path.abspath(repo_root))
    if not os.path.isdir(repo_root):
        raise ValueError("repo-root is not a directory")
    observed_sha = _git(repo_root, "rev-parse", "HEAD").lower()
    observed_tree = _git(repo_root, "rev-parse", "HEAD^{tree}").lower()
    if observed_sha != final_sha:
        raise ValueError("source checkout HEAD does not match FINAL_R8_SHA")
    if observed_tree != final_tree:
        raise ValueError("source checkout tree does not match FINAL_R8_TREE")
    _command(("git", "cat-file", "-e", baseline_sha + "^{commit}"), cwd=repo_root)
    # The handoff document itself is intentionally untracked and is not part
    # of the release.  Tracked modifications are still prohibited.
    _command(("git", "diff", "--quiet"), cwd=repo_root)
    _command(("git", "diff", "--cached", "--quiet"), cwd=repo_root)


def _validate_metadata(metadata, final_sha, final_tree, baseline_sha,
                       baseline_path, candidate_path, workload_hash):
    if metadata.get("repository") != "Chary-yu/fos_coverage_tool":
        raise ValueError("metadata repository must be Chary-yu/fos_coverage_tool")
    if metadata.get("production_host") != "vfoswind":
        raise ValueError("metadata production_host must be vfoswind")
    if str(metadata.get("final_r8_sha") or "").lower() != final_sha:
        raise ValueError("metadata final_r8_sha does not match requested SHA")
    if str(metadata.get("final_r8_tree") or "").lower() != final_tree:
        raise ValueError("metadata final_r8_tree does not match requested tree")
    if str(metadata.get("production_baseline_sha") or "").lower() != baseline_sha:
        raise ValueError("metadata production baseline does not match requested SHA")
    if str(metadata.get("workload_hash") or "") != workload_hash:
        raise ValueError("metadata workload_hash does not match requested workload")
    try:
        budget = float(metadata.get("performance_max_regression_percent", 20))
    except (TypeError, ValueError):
        raise ValueError("metadata performance regression budget is invalid")
    if budget < 0:
        raise ValueError("metadata performance regression budget is negative")
    baseline = validate_revision_artifact(
        baseline_path, baseline_sha, workload_hash, "baseline"
    )
    candidate = validate_revision_artifact(
        candidate_path, final_sha, workload_hash, "candidate"
    )
    if baseline["environment_key"] != candidate["environment_key"]:
        raise ValueError("baseline and Candidate performance environments differ")
    if baseline["payload"].get("workload_id") != candidate["payload"].get(
            "workload_id"):
        raise ValueError("baseline and Candidate workload IDs differ")
    return budget, baseline, candidate


def _b64(data):
    encoded = base64.b64encode(data).decode("ascii")
    return [encoded[index:index + 120] for index in range(0, len(encoded), 120)]


def _shell_quote(value):
    return shlex.quote(str(value))


def _script(metadata, final_sha, final_tree, baseline_sha, bundle_sha,
            baseline_perf_sha, candidate_perf_sha, metadata_sha, bundle_b64,
            baseline_b64, candidate_b64, metadata_b64):
    sha8 = final_sha[:8]
    default_state = "/home/zcyu/coverage_candidate/fos-r8-{}".format(sha8)
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "# FOS R8 exact-source Fast Track; this file contains no download path.",
        "FINAL_R8_SHA={}".format(_shell_quote(final_sha)),
        "FINAL_R8_TREE={}".format(_shell_quote(final_tree)),
        "PRODUCTION_BASELINE_SHA={}".format(_shell_quote(baseline_sha)),
        "BUNDLE_SHA256={}".format(_shell_quote(bundle_sha)),
        "BASELINE_PERFORMANCE_SHA256={}".format(_shell_quote(baseline_perf_sha)),
        "CANDIDATE_PERFORMANCE_SHA256={}".format(_shell_quote(candidate_perf_sha)),
        "METADATA_SHA256={}".format(_shell_quote(metadata_sha)),
        "DEFAULT_STATE_ROOT={}".format(_shell_quote(default_state)),
        "WORKLOAD_HASH={}".format(_shell_quote(metadata["workload_hash"])),
        "",
        "ACTION=run",
        "RESUME=0",
        "VERIFY_ONLY=0",
        "for ARG in \"$@\"; do",
        "  case \"$ARG\" in",
        "    --verify-only) VERIFY_ONLY=1 ;;",
        "    --resume) RESUME=1 ;;",
        "    --status) ACTION=status ;;",
        "    -h|--help)",
        "      echo 'Usage: bash fos_r8_oneclick_release_<sha8>_YYYYMMDD.txt [--verify-only] [--resume] [--status]'",
        "      exit 0",
        "      ;;",
        "    *) echo \"unknown argument: $ARG\" >&2; exit 2 ;;",
        "  esac",
        "done",
        "STATE_ROOT=\"${FOS_R8_STATE_ROOT:-$DEFAULT_STATE_ROOT}\"",
        "CONFIG_PATH=\"${FOS_R8_CONFIG:-/home/zcyu/coverage/onesensor_code-coverage_tool/coverage_config.json}\"",
        "INPUT_ROOT=\"$STATE_ROOT/input\"",
        "SOURCE_CHECKOUT=\"$STATE_ROOT/source\"",
        "BUNDLE_PATH=\"$INPUT_ROOT/source.bundle\"",
        "BASELINE_PERFORMANCE=\"$INPUT_ROOT/baseline-performance.json\"",
        "CANDIDATE_PERFORMANCE=\"$INPUT_ROOT/candidate-performance.json\"",
        "METADATA_PATH=\"$INPUT_ROOT/release-metadata.json\"",
        "",
        "if [ \"$ACTION\" = status ]; then",
        "  if [ -f \"$STATE_ROOT/state.json\" ]; then cat \"$STATE_ROOT/state.json\"; else",
        "    echo '{\"status\":\"NOT_STARTED\"}'",
        "  fi",
        "  if [ -f \"$STATE_ROOT/evidence/final_status.json\" ]; then",
        "    echo; cat \"$STATE_ROOT/evidence/final_status.json\"",
        "  fi",
        "  exit 0",
        "fi",
        "",
        "if ! command -v sha256sum >/dev/null 2>&1; then echo 'sha256sum is required' >&2; exit 1; fi",
        "if ! command -v awk >/dev/null 2>&1; then echo 'awk is required' >&2; exit 1; fi",
        "if ! command -v base64 >/dev/null 2>&1; then echo 'base64 is required' >&2; exit 1; fi",
        "if ! command -v git >/dev/null 2>&1; then echo 'git is required' >&2; exit 1; fi",
        "SELF_SHA256=$(sha256sum \"$0\" | awk '{print $1}')",
        "case \"$SELF_SHA256\" in [0-9a-fA-F][0-9a-fA-F]*) ;; *) echo 'cannot hash the one-click TXT' >&2; exit 1 ;; esac",
        "echo \"R8_TOOL_SHA256=$SELF_SHA256\"",
        "",
        "if [ \"$VERIFY_ONLY\" = 0 ] && [ \"$RESUME\" = 0 ] && [ -f \"$STATE_ROOT/state.json\" ]; then",
        "  echo 'state already exists; use --resume after operator review' >&2",
        "  exit 1",
        "fi",
        "if [ \"$RESUME\" = 1 ] && [ \"$VERIFY_ONLY\" = 0 ] && [ ! -f \"$STATE_ROOT/state.json\" ]; then",
        "  echo '--resume requires an existing R8 state.json' >&2",
        "  exit 1",
        "fi",
        "mkdir -p \"$INPUT_ROOT\"",
        "",
        "extract_payload() {",
        "  local NAME=\"$1\" TARGET=\"$2\" EXPECTED=\"$3\" BEGIN=\"$4\" END=\"$5\"",
        "  if [ -e \"$TARGET\" ]; then",
        "    if [ -L \"$TARGET\" ]; then echo \"refusing symlink input: $TARGET\" >&2; exit 1; fi",
        "  else",
        "    local TMP=\"${TARGET}.tmp.$$\"",
        "    awk -v begin=\"$BEGIN\" -v end=\"$END\" '$0 == begin {inside=1; next} $0 == end {inside=0; exit} inside {print}' \"$0\" | base64 -d > \"$TMP\"",
        "    mv \"$TMP\" \"$TARGET\"",
        "  fi",
        "  local OBSERVED",
        "  OBSERVED=$(sha256sum \"$TARGET\" | awk '{print $1}')",
        "  if [ \"$OBSERVED\" != \"$EXPECTED\" ]; then echo \"$NAME SHA256 mismatch\" >&2; exit 1; fi",
        "}",
        "",
        "extract_payload source-bundle \"$BUNDLE_PATH\" \"$BUNDLE_SHA256\" __R8_SOURCE_BUNDLE_BEGIN__ __R8_SOURCE_BUNDLE_END__",
        "extract_payload baseline-performance \"$BASELINE_PERFORMANCE\" \"$BASELINE_PERFORMANCE_SHA256\" __R8_BASELINE_PERFORMANCE_BEGIN__ __R8_BASELINE_PERFORMANCE_END__",
        "extract_payload candidate-performance \"$CANDIDATE_PERFORMANCE\" \"$CANDIDATE_PERFORMANCE_SHA256\" __R8_CANDIDATE_PERFORMANCE_BEGIN__ __R8_CANDIDATE_PERFORMANCE_END__",
        "extract_payload release-metadata \"$METADATA_PATH\" \"$METADATA_SHA256\" __R8_METADATA_BEGIN__ __R8_METADATA_END__",
        "",
        "VERIFY_ROOT=\"\"",
        "cleanup_verify() {",
        "  if [ -n \"${VERIFY_ROOT:-}\" ] && [ -d \"$VERIFY_ROOT\" ]; then rm -rf -- \"$VERIFY_ROOT\"; fi",
        "}",
        "trap cleanup_verify EXIT",
        "verify_embedded() {",
        "  git bundle verify \"$BUNDLE_PATH\" >/dev/null",
        "  VERIFY_ROOT=$(mktemp -d \"${TMPDIR:-/tmp}/fos-r8-bundle.XXXXXX\")",
        "  git clone \"$BUNDLE_PATH\" \"$VERIFY_ROOT/source\" >/dev/null",
        "  (cd \"$VERIFY_ROOT/source\" && git checkout \"$FINAL_R8_SHA\" >/dev/null)",
        "  (cd \"$VERIFY_ROOT/source\" && test \"$(git rev-parse HEAD)\" = \"$FINAL_R8_SHA\")",
        "  (cd \"$VERIFY_ROOT/source\" && test \"$(git rev-parse HEAD^{tree})\" = \"$FINAL_R8_TREE\")",
        "  (cd \"$VERIFY_ROOT/source\" && git cat-file -e \"${PRODUCTION_BASELINE_SHA}^{commit}\")",
        "  (cd \"$VERIFY_ROOT/source\" && git fsck --full --strict >/dev/null)",
        "  (cd \"$VERIFY_ROOT/source\" && test -z \"$(git status --porcelain --untracked-files=all)\")",
        "}",
        "verify_embedded",
        "",
        "if [ \"$VERIFY_ONLY\" = 1 ]; then",
        "  echo \"EXACT_SOURCE_BUNDLE=PASS\"",
        "  echo \"EXACT_PERFORMANCE_INPUTS=PASS\"",
        "  echo \"PRODUCTION_MUTATION=NONE\"",
        "  exit 0",
        "fi",
        "",
        "if [ ! -e \"$SOURCE_CHECKOUT/.git\" ]; then",
        "  git clone \"$BUNDLE_PATH\" \"$SOURCE_CHECKOUT\" >/dev/null",
        "  (cd \"$SOURCE_CHECKOUT\" && git checkout \"$FINAL_R8_SHA\" >/dev/null)",
        "fi",
        "(cd \"$SOURCE_CHECKOUT\" && test \"$(git rev-parse HEAD)\" = \"$FINAL_R8_SHA\")",
        "(cd \"$SOURCE_CHECKOUT\" && test \"$(git rev-parse HEAD^{tree})\" = \"$FINAL_R8_TREE\")",
        "(cd \"$SOURCE_CHECKOUT\" && test -z \"$(git status --porcelain --untracked-files=all)\")",
        "if [ ! -f \"$CONFIG_PATH\" ]; then echo \"production config is missing: $CONFIG_PATH\" >&2; exit 1; fi",
        "PYTHON_BIN=\"${FOS_R8_PYTHON:-python3.6}\"",
        "if ! command -v \"$PYTHON_BIN\" >/dev/null 2>&1 && [ ! -x \"$PYTHON_BIN\" ]; then",
        "  echo \"Python 3.6 executable is required: $PYTHON_BIN\" >&2; exit 1",
        "fi",
        "if ! \"$PYTHON_BIN\" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 6) else 1)'; then",
        "  echo 'R8 conductor must run under Python 3.6' >&2; exit 1",
        "fi",
        "echo 'PRODUCTION_MUTATION=NONE until APPLY R8'",
        "set +e",
        "\"$PYTHON_BIN\" \"$SOURCE_CHECKOUT/scripts/release/fos_r8_conductor.py\" \\",
        "  --repo-root \"$SOURCE_CHECKOUT\" \\",
        "  --config \"$CONFIG_PATH\" \\",
        "  --state-root \"$STATE_ROOT\" \\",
        "  --metadata \"$METADATA_PATH\" \\",
        "  --source-bundle \"$BUNDLE_PATH\" \\",
        "  --baseline-performance \"$BASELINE_PERFORMANCE\" \\",
        "  --candidate-performance \"$CANDIDATE_PERFORMANCE\" \\",
        "  $(if [ \"$RESUME\" = 1 ]; then echo --resume; fi)",
        "CODE=$?",
        "set -e",
        "echo \"R8_CONDUCTOR_EXIT=$CODE\"",
        "if [ -f \"$STATE_ROOT/evidence/final_status.json\" ]; then cat \"$STATE_ROOT/evidence/final_status.json\"; fi",
        "exit \"$CODE\"",
        "",
        "__R8_SOURCE_BUNDLE_BEGIN__",
    ]
    lines.extend(bundle_b64)
    lines.extend([
        "__R8_SOURCE_BUNDLE_END__",
        "__R8_BASELINE_PERFORMANCE_BEGIN__",
    ])
    lines.extend(baseline_b64)
    lines.extend([
        "__R8_BASELINE_PERFORMANCE_END__",
        "__R8_CANDIDATE_PERFORMANCE_BEGIN__",
    ])
    lines.extend(candidate_b64)
    lines.extend([
        "__R8_CANDIDATE_PERFORMANCE_END__",
        "__R8_METADATA_BEGIN__",
    ])
    lines.extend(metadata_b64)
    lines.extend(["__R8_METADATA_END__", ""])
    return "\n".join(lines)


def build(args):
    repo_root = os.path.realpath(os.path.abspath(args.repo_root))
    final_sha = _exact_sha(args.final_sha, "final_r8_sha", SHA40)
    final_tree = _exact_sha(args.final_tree, "final_r8_tree", SHA40)
    baseline_sha = _exact_sha(
        args.baseline_sha, "production_baseline_sha", SHA40
    )
    _validate_source_checkout(repo_root, final_sha, final_tree, baseline_sha)
    observed_tree = _git(repo_root, "rev-parse", final_sha + "^{tree}").lower()
    if observed_tree != final_tree:
        raise ValueError("requested FINAL_R8_TREE does not match Git")
    bundle_path = os.path.realpath(os.path.abspath(args.source_bundle))
    _validate_bundle(bundle_path, final_sha, final_tree, baseline_sha)
    workload_hash = str(args.workload_hash or "").strip()
    if not workload_hash:
        raise ValueError("workload hash is required")
    baseline_path = os.path.realpath(os.path.abspath(args.baseline_performance))
    candidate_path = os.path.realpath(os.path.abspath(args.candidate_performance))
    metadata = _read_json(args.metadata, "release metadata")
    budget, baseline, candidate = _validate_metadata(
        metadata, final_sha, final_tree, baseline_sha,
        baseline_path, candidate_path, workload_hash,
    )
    metadata = dict(metadata)
    metadata.update({
        "schema_version": 1,
        "final_r8_sha": final_sha,
        "final_r8_tree": final_tree,
        "production_baseline_sha": baseline_sha,
        "workload_hash": workload_hash,
        "performance_max_regression_percent": budget,
        "source_bundle_sha256": _sha256(bundle_path),
        "baseline_performance_sha256": _sha256(baseline_path),
        "candidate_performance_sha256": _sha256(candidate_path),
        "baseline_environment_identity": baseline["payload"].get(
            "environment_identity"
        ),
        "candidate_environment_identity": candidate["payload"].get(
            "environment_identity"
        ),
        "generated_by": "scripts/release/build_oneclick_release.py",
    })
    metadata_bytes = json.dumps(
        metadata, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8") + b"\n"
    metadata_sha = hashlib.sha256(metadata_bytes).hexdigest()
    output = os.path.abspath(args.output)
    with open(bundle_path, "rb") as stream:
        bundle_data = stream.read()
    with open(baseline_path, "rb") as stream:
        baseline_data = stream.read()
    with open(candidate_path, "rb") as stream:
        candidate_data = stream.read()
    script = _script(
        metadata, final_sha, final_tree, baseline_sha,
        metadata["source_bundle_sha256"], metadata["baseline_performance_sha256"],
        metadata["candidate_performance_sha256"], metadata_sha,
        _b64(bundle_data),
        _b64(baseline_data),
        _b64(candidate_data),
        _b64(metadata_bytes),
    )
    parent = os.path.dirname(output)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    temporary = output + ".tmp-{}".format(os.getpid())
    with open(temporary, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(script)
        stream.write("\n")
    os.replace(temporary, output)
    return {
        "status": "PASSED",
        "output": output,
        "sha256": _sha256(output),
        "final_r8_sha": final_sha,
        "final_r8_tree": final_tree,
        "production_baseline_sha": baseline_sha,
        "source_bundle_sha256": metadata["source_bundle_sha256"],
        "baseline_performance_sha256": metadata["baseline_performance_sha256"],
        "candidate_performance_sha256": metadata["candidate_performance_sha256"],
        "metadata_sha256": metadata_sha,
        "workload_hash": workload_hash,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="build_oneclick_release.py")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--final-sha", required=True)
    parser.add_argument("--final-tree", required=True)
    parser.add_argument("--baseline-sha", required=True)
    parser.add_argument("--source-bundle", required=True)
    parser.add_argument("--baseline-performance", required=True)
    parser.add_argument("--candidate-performance", required=True)
    parser.add_argument("--workload-hash", required=True)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = build(args)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print("one-click release generation failed: {}".format(exc),
              file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
