#!/usr/bin/env python3
"""Deterministic local release gate and offline evidence bundle producer.

This command is the repository-local replacement for hosted release checks.
It creates a clean exact-revision source snapshot, records the environment,
runs only the release-focused tests and static regressions, generates and
self-tests a manifest-bound operator, runs the local compatibility gate, and
emits an offline-verifiable evidence directory.  It never contacts a remote
service or a production host.
"""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
TEST_MODULES = (
    "tests.release.test_offline_operator_trust",
    "tests.release.test_flat_production_candidate_operator",
    "tests.release.test_production_candidate_build",
    "tests.release.test_immutable_release_publication",
    "tests.release.test_fos_r8_orchestration",
    "tests.release.test_release_readiness",
    "tests.release.test_production_ready_evidence_join",
    "tests.release.test_upgrade_manifest",
    "tests.release.test_release_input_manifest",
    "tests.release.test_local_first_release",
)
SHA40 = re.compile(r"^[0-9a-fA-F]{40}$")
SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
SAFE_SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
COMPATIBILITY_ALLOWED_STATUSES = (
    "PASS", "NOT_APPLICABLE", "DEFERRED_TO_TARGET_RUNTIME",
)
FORBIDDEN_RUNTIME_PATTERNS = (
    (r"\bssh(?:\s|$)", "ssh"),
    (r"\bscp(?:\s|$)", "scp"),
    (r"\bsftp(?:\s|$)", "sftp"),
    (r"\bcurl(?:\s|$)", "curl"),
    (r"\bwget(?:\s|$)", "wget"),
    (r"\bsystemctl(?:\s|$)", "systemctl"),
    (r"\bgh\s+run\b", "hosted-run-query"),
    (r"api\.github\.com", "hosted-api"),
    (r"\b(?:GITHUB_TOKEN|GH_TOKEN)\b", "release-token"),
)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _atomic_json(path, value):
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


def _run(argv, cwd=None, env=None, timeout=600):
    started = time.time()
    try:
        process = subprocess.Popen(
            list(argv), cwd=cwd, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        stdout, _ = process.communicate(timeout=timeout)
        return process.returncode, stdout.decode("utf-8", "replace"), \
            time.time() - started
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        try:
            process.kill()
            stdout, _ = process.communicate()
            output = stdout.decode("utf-8", "replace")
        except (UnboundLocalError, OSError):
            output = ""
        return 127, "{}\n{}".format(exc, output), time.time() - started


def _git(repo_root, *args, timeout=120):
    code, output, duration = _run(("git",) + tuple(args), cwd=repo_root,
                                  timeout=timeout)
    if code != 0:
        raise ValueError("git {} failed: {}".format(" ".join(args), output[-2000:]))
    return output.strip(), duration


def _safe_output(repo_root, output):
    repo_root = os.path.realpath(os.path.abspath(repo_root))
    output = os.path.abspath(str(output))
    allowed = False
    for root in (os.path.join(repo_root, ".artifacts"), tempfile.gettempdir()):
        try:
            if os.path.commonpath((os.path.realpath(root), os.path.realpath(output))) == \
                    os.path.realpath(root):
                allowed = True
                break
        except ValueError:
            continue
    if not allowed:
        raise ValueError("local evidence output must be inside repository .artifacts or /tmp")
    if os.path.islink(output):
        raise ValueError("local evidence output may not be a symlink")
    if not os.path.isdir(output):
        os.makedirs(output)
    return output


def _clean_env():
    environment = dict(os.environ)
    for name in (
            "GITHUB_TOKEN", "GH_TOKEN", "ACTIONS_RUNTIME_TOKEN",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL"):
        environment.pop(name, None)
    return environment


def _source_identity(repo_root, requested_sha, output):
    head, _ = _git(repo_root, "rev-parse", "HEAD")
    tree, _ = _git(repo_root, "rev-parse", "HEAD^{tree}")
    status, _ = _git(repo_root, "status", "--porcelain", "--untracked-files=all")
    _remote_code, remote, _remote_duration = _run(
        ("git", "config", "--get", "remote.origin.url"), cwd=repo_root
    )
    source = {
        "schema_version": 1,
        "requested_source_sha": requested_sha,
        "head_sha": head.lower(),
        "tree_sha": tree.lower(),
        "worktree_clean": not bool(status.strip()),
        "worktree_status": status.splitlines(),
        "repository": str(remote).strip(),
        "clean_checkout": False,
        "production_mutation": "NONE",
    }
    if head.lower() != requested_sha.lower():
        source["blocker"] = "HEAD does not match --source-sha"
    if not SHA40.fullmatch(head) or not SHA40.fullmatch(tree):
        source["blocker"] = "HEAD/tree identity is not exact"
    with tempfile.TemporaryDirectory(prefix="local-release-clean-checkout-") as clean:
        checkout = os.path.join(clean, "checkout")
        code, output_text, duration = _run(
            ("git", "clone", "-q", "--no-hardlinks", repo_root, checkout),
            timeout=180,
        )
        if code == 0:
            code, output_text, _ = _run(
                ("git", "checkout", "--detach", "-q", requested_sha),
                cwd=checkout, timeout=120,
            )
        clean_status = ""
        if code == 0:
            clean_status, _ = _git(checkout, "status", "--porcelain",
                                    "--untracked-files=all")
        source.update({
            "clean_checkout": code == 0 and not clean_status,
            "clean_checkout_duration_seconds": duration,
            "clean_checkout_output_tail": output_text[-2000:],
        })
    bundle = os.path.join(output, "source.bundle")
    tooling_bundle = os.path.join(output, "tooling.bundle")
    for path in (bundle, tooling_bundle):
        code, output_text, _ = _run(
            ("git", "bundle", "create", path, "HEAD"),
            cwd=repo_root, timeout=180,
        )
        if code != 0:
            raise ValueError("git bundle creation failed: {}".format(output_text[-2000:]))
        _git(repo_root, "bundle", "verify", path)
    source.update({
        "source_bundle_path": bundle,
        "source_bundle_sha256": _sha256(bundle),
        "tooling_bundle_path": tooling_bundle,
        "tooling_bundle_sha256": _sha256(tooling_bundle),
    })
    _atomic_json(os.path.join(output, "source_identity.json"), source)
    return source


def _source_identity_ready(identity, requested_sha):
    """Return whether the gate may trust the local source checkout identity."""
    if not isinstance(identity, dict):
        return False
    requested_sha = str(requested_sha or "").strip().lower()
    head_sha = str(identity.get("head_sha") or "").strip().lower()
    tree_sha = str(identity.get("tree_sha") or "").strip().lower()
    return bool(
        SHA40.fullmatch(requested_sha) and requested_sha != "0" * 40 and
        SHA40.fullmatch(head_sha) and head_sha == requested_sha and
        SHA40.fullmatch(tree_sha) and identity.get("worktree_clean") is True and
        identity.get("clean_checkout") is True
    )


def compatibility_scope(payload):
    """Return the release scope allowed by compatibility evidence.

    A deferred target-runtime check is explicitly sufficient for isolated
    Candidate staging only.  It never silently becomes a publication or
    cutover decision.
    """
    payload = dict(payload or {})
    statuses = (
        str(payload.get("PYTHON36_COMPAT") or ""),
        str(payload.get("MARIADB55_COMPAT") or ""),
    )
    invalid = [status for status in statuses
               if status not in COMPATIBILITY_ALLOWED_STATUSES + ("FAIL",)]
    failed = "FAIL" in statuses or invalid
    deferred = "DEFERRED_TO_TARGET_RUNTIME" in statuses
    staging_allowed = not bool(failed)
    publication_allowed = staging_allowed and not deferred
    return {
        "isolated_candidate_staging_allowed": staging_allowed,
        "publication_allowed": publication_allowed,
        "cutover_allowed": publication_allowed,
        "deferred": deferred,
        "invalid_statuses": invalid,
        "required_next_step": (
            "READY_FOR_MANUAL_VFOS_COMPATIBILITY_PREFLIGHT"
            if deferred else ""
        ),
    }


def _environment_identity(repo_root):
    _git_code, git_version, _git_duration = _run(
        ("git", "--version"), cwd=repo_root
    )
    docker = shutil.which("docker")
    docker_version = ""
    if docker:
        _code, docker_version, _ = _run((docker, "--version"), timeout=30)
    return {
        "schema_version": 1,
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "git_version": git_version.strip(),
        "docker_version": docker_version.strip(),
        "docker_available": bool(docker),
        "os": platform.system(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "production_mutation": "NONE",
    }


def _test_record(repo_root, modules, output_path):
    command = [sys.executable, "-m", "unittest"] + list(modules)
    code, output, duration = _run(command, cwd=repo_root, env=_clean_env(), timeout=1800)
    match = re.search(r"Ran\s+(\d+)\s+tests?", output)
    skipped = len(re.findall(r"\bskipped\b", output, re.IGNORECASE))
    record = {
        "schema_version": 1,
        "command": command,
        "command_text": "python3 -m unittest " + " ".join(modules),
        "exit_code": code,
        "passed": code == 0,
        "failed": code != 0,
        "skipped": skipped,
        "test_count": int(match.group(1)) if match else 0,
        "duration_seconds": duration,
        "output_tail": output[-20000:],
        "production_mutation": "NONE",
    }
    _atomic_json(output_path, record)
    return record


def _script_record(repo_root, command, output_path, label):
    code, output, duration = _run(command, cwd=repo_root, env=_clean_env(), timeout=1800)
    record = {
        "schema_version": 1,
        "label": label,
        "command": list(command),
        "command_text": " ".join(str(item) for item in command),
        "exit_code": code,
        "passed": code == 0,
        "duration_seconds": duration,
        "output_tail": output[-20000:],
        "production_mutation": "NONE",
    }
    _atomic_json(output_path, record)
    return record


def _security_scan(repo_root, operator_path):
    payload_markers = {
        "__RUNTIME_BEGIN__", "__RUNTIME_END__",
        "__MANIFEST_BEGIN__", "__MANIFEST_END__",
        "__SOURCE_BUNDLE_BEGIN__", "__SOURCE_BUNDLE_END__",
        "__TOOLING_BUNDLE_BEGIN__", "__TOOLING_BUNDLE_END__",
    }

    def executable_operator_text(value):
        """Exclude opaque Base64 payloads while retaining shell text.

        Git bundles and the embedded runtime are binary/encoded data.  A
        lexical scan over those bytes can report a random ``scp``/``ssh``
        substring as an executable command.  The runtime source is scanned
        separately above; this view is only for the generated shell wrapper.
        """
        lines = []
        in_payload = False
        for line in value.splitlines():
            marker = line.strip()
            if marker in payload_markers:
                in_payload = marker.endswith("_BEGIN__")
                continue
            if not in_payload:
                lines.append(line)
        return "\n".join(lines)

    paths = (
        "app/release_trust_policy.py",
        "app/candidate_artifact.py",
        "app/release_publication.py",
        "scripts/release/build_production_candidate_artifact.py",
        "scripts/release/build_flat_production_candidate_artifact.py",
        "scripts/release/publish_release.py",
        "scripts/release/release_input_manifest.py",
        "scripts/release/flat_candidate_operator_runtime.py",
        "scripts/release/generate_flat_production_candidate_operator.py",
        "scripts/diagnostics/production_release_authority.py",
    )
    findings = []
    for relative in paths:
        path = os.path.join(repo_root, relative)
        if not os.path.isfile(path):
            findings.append({"path": relative, "finding": "missing"})
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            text = stream.read()
        for pattern, label in FORBIDDEN_RUNTIME_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                findings.append({"path": relative, "finding": label})
    if operator_path and os.path.isfile(operator_path):
        with open(operator_path, "r", encoding="utf-8", errors="replace") as stream:
            operator = executable_operator_text(stream.read())
        for pattern, label in FORBIDDEN_RUNTIME_PATTERNS:
            if re.search(pattern, operator, re.IGNORECASE):
                findings.append({"path": os.path.basename(operator_path), "finding": label})
    return {
        "schema_version": 1,
        "status": "PASS" if not findings else "FAIL",
        "findings": findings,
        "network_access": "NONE",
        "production_mutation": "NONE",
    }


def build_evidence_manifest(root, source_sha, session_id):
    """Write the detached evidence manifest and SHA256SUMS file."""
    root = os.path.abspath(str(root))
    artifacts = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(dirnames)
        for name in sorted(filenames):
            path = os.path.join(directory, name)
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            if relative in ("evidence_manifest.json", "SHA256SUMS"):
                continue
            if os.path.islink(path) or not os.path.isfile(path):
                raise ValueError("evidence artifact is not a regular file: {}".format(relative))
            artifacts.append({
                "path": relative,
                "size": os.path.getsize(path),
                "sha256": _sha256(path),
                "producer": "local_release_gate",
                "source_sha": source_sha,
                "session_id": session_id,
            })
    artifacts.sort(key=lambda item: item["path"])
    payload = {
        "schema_version": 1,
        "evidence_class": "local_release_evidence_bundle",
        "source_sha": source_sha,
        "session_id": session_id,
        "production_mutation": "NONE",
        "artifacts": artifacts,
        "excluded_from_manifest": ["evidence_manifest.json", "SHA256SUMS"],
    }
    path = os.path.join(root, "evidence_manifest.json")
    _atomic_json(path, payload)
    sums_path = os.path.join(root, "SHA256SUMS")
    with open(sums_path + ".tmp-{}".format(os.getpid()), "w", encoding="ascii") as stream:
        for item in artifacts:
            stream.write("{}  {}\n".format(item["sha256"], item["path"]))
        stream.write("{}  evidence_manifest.json\n".format(_sha256(path)))
    os.replace(sums_path + ".tmp-{}".format(os.getpid()), sums_path)
    return path


def verify_evidence_manifest(root, manifest_path=None):
    """Return False if any listed evidence bytes or metadata are tampered."""
    root = os.path.abspath(str(root))
    path = manifest_path or os.path.join(root, "evidence_manifest.json")
    try:
        with open(path, "r", encoding="utf-8") as stream:
            payload = json.load(stream)
        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, list):
            return False
        listed = set()
        for item in artifacts:
            relative = str(item.get("path") or "")
            if not relative or relative in listed or relative.startswith("/") or \
                    ".." in relative.split("/"):
                return False
            listed.add(relative)
            target = os.path.join(root, *relative.split("/"))
            if os.path.realpath(target) != os.path.abspath(target) or \
                    os.path.islink(target) or not os.path.isfile(target):
                return False
            if int(item.get("size")) != os.path.getsize(target) or \
                    item.get("sha256") != _sha256(target):
                return False
            if item.get("producer") != "local_release_gate" or \
                    item.get("source_sha") != payload.get("source_sha") or \
                    item.get("session_id") != payload.get("session_id"):
                return False
        for directory, _dirnames, filenames in os.walk(root, followlinks=False):
            for name in filenames:
                relative = os.path.relpath(
                    os.path.join(directory, name), root
                ).replace(os.sep, "/")
                if relative not in listed and relative not in (
                        "evidence_manifest.json", "SHA256SUMS"):
                    return False
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _make_release_input_manifest(source, output, session_id, baseline_sha):
    from scripts.release.release_input_manifest import (
        build_release_input_manifest, write_release_input_manifest,
    )
    digest_literal = lambda label: hashlib.sha256(label.encode("utf-8")).hexdigest()
    values = {
        "schema_version": 1,
        "repository": "Chary-yu/fos_coverage_tool",
        "source_commit_sha": source["head_sha"],
        "source_tree_sha": source["tree_sha"],
        "source_bundle_sha256": source["source_bundle_sha256"],
        "tooling_commit_sha": source["head_sha"],
        "tooling_tree_sha": source["tree_sha"],
        "tooling_bundle_sha256": source["tooling_bundle_sha256"],
        "production_host": "local-validation",
        "baseline_commit_sha": baseline_sha or source["head_sha"],
        "baseline_attempt_id": "local-validation-baseline",
        "baseline_tree_sha256": digest_literal("local-validation-tree"),
        "baseline_manifest_sha256": digest_literal("local-validation-manifest"),
        "baseline_adoption_identity_sha256": digest_literal("local-validation-adoption"),
        "project_name": "FOS_V6R2",
        "candidate_parent": os.path.join(output, "candidate-parent"),
        "release_trust_mode": "offline_operator",
        "candidate_build_mode": "validation_fixture",
        "production_publishable": False,
        "validation_session_id": session_id,
    }
    manifest = build_release_input_manifest(values)
    path = os.path.join(output, "release_input_manifest.json")
    digest = write_release_input_manifest(
        path, manifest, os.path.join(output, "release_input_manifest.sha256")
    )
    return path, digest


def run_gate(repo_root, source_sha, output, session_id="", test_modules=None,
             baseline_sha=""):
    """Run all local release checks and return the machine-readable decision."""
    repo_root = os.path.abspath(repo_root)
    source_sha = str(source_sha or "").strip().lower()
    if not SHA40.fullmatch(source_sha) or source_sha == "0" * 40:
        raise ValueError("source_sha must be an exact non-zero commit SHA")
    session_id = session_id or "local-{}".format(source_sha[:12])
    if not SAFE_SESSION.fullmatch(session_id):
        raise ValueError("session_id contains unsafe characters")
    baseline_sha = str(baseline_sha or source_sha).strip().lower()
    if not SHA40.fullmatch(baseline_sha) or baseline_sha == "0" * 40:
        raise ValueError("baseline_sha must be an exact non-zero commit SHA")
    output = _safe_output(repo_root, output)
    source = _source_identity(repo_root, source_sha, output)
    _atomic_json(os.path.join(output, "environment_identity.json"),
                 _environment_identity(repo_root))
    manifest_path, manifest_sha = _make_release_input_manifest(
        source, output, session_id, baseline_sha
    )
    from scripts.release.generate_flat_production_candidate_operator import generate
    operator_path = os.path.join(output, "offline_operator.txt")
    operator_result = {}
    operator_error = ""
    try:
        operator_result = generate(
            manifest_path, source["source_bundle_path"],
            source["tooling_bundle_path"], operator_path,
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        operator_error = str(exc)
    operator_record = {
        "schema_version": 1,
        "operator_file": operator_path,
        "operator_generated": bool(operator_result),
        "operator_generation": operator_result,
        "release_input_manifest_sha256": manifest_sha,
        "production_mutation": "NONE",
    }
    if operator_error:
        operator_record.update({"status": "FAIL", "error": operator_error})
    else:
        bash_code, bash_output, bash_duration = _run(
            ("bash", "-n", operator_path), cwd=repo_root, env=_clean_env(), timeout=120
        )
        self_code, self_output, self_duration = _run(
            ("bash", operator_path, "--self-test"),
            cwd=repo_root, env=_clean_env(), timeout=300,
        )
        operator_record.update({
            "status": "PASS" if bash_code == 0 and self_code == 0 and
            "OPERATOR_SELF_TEST=PASS" in self_output and
            "PRODUCTION_MUTATION=NONE" in self_output else "FAIL",
            "bash_syntax_exit_code": bash_code,
            "bash_syntax_output_tail": bash_output[-4000:],
            "bash_syntax_duration_seconds": bash_duration,
            "self_test_exit_code": self_code,
            "self_test_output_tail": self_output[-12000:],
            "self_test_duration_seconds": self_duration,
        })
    _atomic_json(os.path.join(output, "operator_self_test.json"), operator_record)
    modules = tuple(test_modules or TEST_MODULES)
    tests = _test_record(repo_root, modules, os.path.join(output, "targeted_tests.json"))
    compatibility = _script_record(
        repo_root,
        (sys.executable, "scripts/release/local_compatibility_gate.py",
         "--repo-root", repo_root, "--output",
         os.path.join(output, "compatibility_tests.json"),
         "--source-sha", source_sha,
         "--baseline-sha", baseline_sha),
        os.path.join(output, "compatibility_gate_command.json"),
        "local Python 3.6/MariaDB 5.5 compatibility",
    )
    compatibility_payload = {}
    try:
        with open(os.path.join(output, "compatibility_tests.json"), encoding="utf-8") as stream:
            compatibility_payload = json.load(stream)
    except (OSError, ValueError, TypeError):
        compatibility_payload = {"status": "FAIL"}
    if isinstance(compatibility_payload.get("compatibility_surface"), dict):
        _atomic_json(
            os.path.join(output, "compatibility_surface.json"),
            compatibility_payload["compatibility_surface"],
        )
    regressions = _script_record(
        repo_root,
        (sys.executable, "scripts/validate_release_orchestration.py",
         "--repo-root", repo_root, "--output",
         os.path.join(output, "release_regressions.json")),
        os.path.join(output, "release_regressions_command.json"),
        "release orchestration static validation",
    )
    regression_suite = _script_record(
        repo_root,
        (sys.executable, "scripts/validate_release_orchestration_regressions.py",
         "--repo-root", repo_root, "--output",
         os.path.join(output, "release_orchestration_regressions.json")),
        os.path.join(output, "release_orchestration_regressions_command.json"),
        "release orchestration targeted regressions",
    )
    security = _security_scan(repo_root, operator_path)
    _atomic_json(os.path.join(output, "security_scan.json"), security)
    from scripts.diagnostics.production_release_authority import evaluate
    authority = evaluate(repo_root)
    authority_ok = authority.get("LOCAL_FIRST_AUTHORITY_READY") == "YES"
    local_status = "PASS"
    if not _source_identity_ready(source, source_sha) or not authority_ok or \
            not tests["passed"] or operator_record.get("status") != "PASS" or \
            not security.get("status") == "PASS" or not regressions["passed"] or \
            not regression_suite["passed"]:
        local_status = "FAIL"
    compatibility_scope_result = compatibility_scope(compatibility_payload)
    if not compatibility_scope_result["isolated_candidate_staging_allowed"]:
        local_status = "FAIL"
    gate = {
        "schema_version": 1,
        "local_release_gate": local_status,
        "source_sha": source_sha,
        "source_tree_sha": source.get("tree_sha"),
        "session_id": session_id,
        "release_input_manifest_sha256": manifest_sha,
        "source_identity": source,
        "authority": authority,
        "targeted_tests": tests,
        "compatibility": compatibility_payload,
        "compatibility_scope": compatibility_scope_result,
        "release_regressions": regressions,
        "release_orchestration_regressions": regression_suite,
        "security": security,
        "operator_self_test": operator_record,
        "production_mutation": "NONE",
    }
    _atomic_json(os.path.join(output, "local_release_gate.json"), gate)
    evidence_manifest_path = build_evidence_manifest(output, source_sha, session_id)
    gate["evidence_manifest"] = evidence_manifest_path
    gate["evidence_manifest_sha256"] = _sha256(evidence_manifest_path)
    # The gate record is intentionally hashed before the evidence manifest is
    # finalized. Updating it afterward would invalidate its own evidence entry.
    return gate


def main(argv=None):
    parser = argparse.ArgumentParser(prog="local_release_gate.py")
    parser.add_argument("--repo-root", default=ROOT)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--baseline-sha", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--session-id", default="")
    parser.add_argument("--test-module", action="append", dest="test_modules")
    args = parser.parse_args(argv)
    try:
        result = run_gate(
            args.repo_root, args.source_sha, args.output,
            session_id=args.session_id, test_modules=args.test_modules,
            baseline_sha=args.baseline_sha,
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        print("LOCAL_RELEASE_GATE=FAIL", file=sys.stderr)
        print("BLOCKER={}".format(exc), file=sys.stderr)
        return 1
    print("LOCAL_RELEASE_GATE={}".format(result["local_release_gate"]))
    print("RELEASE_INPUT_MANIFEST_SHA256={}".format(
        result["release_input_manifest_sha256"]
    ))
    print("EVIDENCE_MANIFEST_SHA256={}".format(result["evidence_manifest_sha256"]))
    print("PRODUCTION_MUTATION=NONE")
    return 0 if result["local_release_gate"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
