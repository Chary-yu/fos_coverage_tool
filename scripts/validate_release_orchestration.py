#!/usr/bin/env python3
"""Static, offline validation of the R8 release orchestration contract.

This checker is intentionally independent of vfoswind.  It verifies the
source-level fail-closed boundaries, the complete evidence namespace, and an
optional generated TXT syntax/integrity surface without contacting GitHub,
MariaDB, Nginx, systemd, or a production host.
"""

from __future__ import print_function

import argparse
import json
import os
import re
import subprocess
import sys


REQUIRED_FILES = (
    "scripts/release/fos_r8_conductor.py",
    "scripts/release/build_oneclick_release.py",
    "scripts/release/build_flat_production_candidate_artifact.py",
    "scripts/diagnostics/release_performance_revision.js",
    "scripts/upgrade/performance_evidence.py",
    "scripts/upgrade/run_upgrade.py",
    "scripts/release/current_adoption.py",
    "scripts/release/prepare_legacy_flat_adoption.py",
)
REQUIRED_EVIDENCE = (
    "input_integrity.json", "readiness.json", "production_baseline.json",
    "release_identity.json", "candidate_artifact_manifest.json",
    "offline_operator_evidence.json", "release_manifest.json",
    "validation_session_manifest.json", "backup_evidence.json",
    "restore_rehearsal.json", "database_generation.json",
    "disposable_target.json", "migration_evidence.json",
    "candidate_gateway_preflight.json", "candidate_runtime_binding.json",
    "candidate_server_gate.json", "operator_browser_observation.json",
    "candidate_auth_probe_evidence.json", "release_performance_ab.json",
    "candidate_browser_evidence.json", "rollback_evidence.json",
    "pre_cutover_ready.json", "cutover_evidence.json",
    "post_open_verification.json", "final_status.json",
    "sha256_inventory.json",
)
FORBIDDEN_RUNTIME_PATTERNS = (
    (r"\bgit\s+fetch\b", "git fetch"),
    (r"\bgit\s+pull\b", "git pull"),
    (r"\bssh(?:\s|$)", "ssh"),
    (r"\bscp(?:\s|$)", "scp"),
    (r"\bsftp(?:\s|$)", "sftp"),
    (r"\bcurl(?:\s|$)", "curl"),
    (r"\bwget(?:\s|$)", "wget"),
    (r"\bnpm\s+(?:install|ci)\b", "npm install/npm ci"),
)


def _read(path):
    with open(path, "r", encoding="utf-8") as stream:
        return stream.read()


def _check(checks, name, passed, detail):
    checks.append({
        "name": name,
        "status": "PASSED" if passed else "FAILED",
        "detail": detail,
    })
    return passed


def _run(argv, cwd):
    result = subprocess.Popen(
        list(argv), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    stdout, stderr = result.communicate()
    return result.returncode, stdout.decode("utf-8", "replace"), \
        stderr.decode("utf-8", "replace")


def validate(repo_root, tool_path="", bundle_path=""):
    repo_root = os.path.realpath(os.path.abspath(repo_root))
    checks = []
    failures = []
    for relative in REQUIRED_FILES:
        passed = os.path.isfile(os.path.join(repo_root, relative))
        if not _check(checks, "required_file:" + relative, passed,
                      "present" if passed else "missing"):
            failures.append(relative)

    conductor_path = os.path.join(repo_root, "scripts/release/fos_r8_conductor.py")
    run_upgrade_path = os.path.join(repo_root, "scripts/upgrade/run_upgrade.py")
    flat_path = os.path.join(
        repo_root, "scripts/release/prepare_legacy_flat_adoption.py"
    )
    perf_path = os.path.join(repo_root, "scripts/upgrade/performance_evidence.py")
    generator_path = os.path.join(
        repo_root, "scripts/release/build_oneclick_release.py"
    )
    try:
        conductor = _read(conductor_path)
        run_upgrade = _read(run_upgrade_path)
        flat = _read(flat_path)
        perf = _read(perf_path)
        generator = _read(generator_path)
    except (OSError, IOError) as exc:
        failures.append(str(exc))
        conductor = run_upgrade = flat = perf = generator = ""

    evidence_match = re.search(r"EVIDENCE_NAMES\s*=\s*\((.*?)\)\s*\n", conductor,
                               re.DOTALL)
    declared_evidence = set(re.findall(r"[\"']([a-z0-9_]+\.json)[\"']",
                                       evidence_match.group(1) if evidence_match else ""))
    evidence_ok = set(REQUIRED_EVIDENCE).issubset(declared_evidence)
    if not _check(checks, "evidence_namespace", evidence_ok,
                  "{} required records declared".format(
                      len(set(REQUIRED_EVIDENCE) & declared_evidence))):
        failures.append("evidence namespace is incomplete")

    phase_ok = all(
        token in conductor for token in (
            '"A", "RUNNING"', '"B", "RUNNING"', '"C", "RUNNING"',
            '"D", "RUNNING"', '"F", "PASSED"', '"FAILED"',
        )
    ) and "Phase D" in run_upgrade and "post_open_serving" in run_upgrade
    if not _check(checks, "phase_a_to_f_fail_closed", phase_ok,
                  "conductor and upgrade lifecycle expose A-F checkpoints"):
        failures.append("phase A-F checkpoints are incomplete")

    browser_gate_ok = all(token in run_upgrade for token in (
        "BROWSER READY", "candidate_browser_url", "candidate_artifact_sha256",
        "served_root_sha256", "release_validation_session_id",
    ))
    if not _check(checks, "browser_auth_session_binding", browser_gate_ok,
                  "browser/session/publication identities are required"):
        failures.append("browser/session binding is incomplete")

    performance_ok = all(token in perf for token in (
        "release_performance_revision", "single_revision", "synthetic",
        "Tier_A_1k", "Tier_B_10k", "Tier_C_50k", "Tier_D_100k",
        "coverage_virtual_scroll_100k",
    )) and "join_release_performance" in run_upgrade
    if not _check(checks, "exact_revision_performance_join", performance_ok,
                  "revision source artifacts and Python-side A/B join are present"):
        failures.append("performance join contract is incomplete")

    flat_ok = all(token in flat for token in (
        "flat_source_binding", "legacy_source_tree_sha256",
        "legacy_source_file_count", "legacy_source_total_size",
        "legacy_release_identity_sha256", "verify_flat_source_binding",
    ))
    if not _check(checks, "flat_deterministic_binding", flat_ok,
                  "Flat Root binding excludes generated publication metadata"):
        failures.append("Flat Root binding contract is incomplete")

    apply_pos = run_upgrade.rfind(
        "if not self._require_production_mutation_confirmation"
    )
    ready_pos = run_upgrade.rfind(
        "ready, unmet = self._validate_pre_cutover_ready"
    )
    phase_d_pos = run_upgrade.rfind("self._phase_d_entered = True")
    boundary_ok = apply_pos >= 0 and ready_pos >= 0 and phase_d_pos >= 0 and \
        ready_pos < apply_pos < phase_d_pos and \
        "PRODUCTION_MUTATION" in run_upgrade
    if not _check(checks, "production_mutation_boundary", boundary_ok,
                  "PRE_CUTOVER_READY precedes APPLY R8 and Phase D"):
        failures.append("production mutation boundary ordering is invalid")

    source_contract_ok = "git bundle" in generator and \
        "git fsck" in generator and "PRODUCTION_MUTATION=NONE" in generator and \
        "python3.6" in generator
    if not _check(checks, "offline_source_transfer", source_contract_ok,
                  "generator embeds bundle and performs local exact checkout checks"):
        failures.append("offline source-transfer contract is incomplete")

    for path, label in (
            (conductor_path, "conductor"), (run_upgrade_path, "upgrade"),
            (flat_path, "flat binding"), (perf_path, "performance join"),
            (generator_path, "one-click generator")):
        try:
            compile(_read(path), path, "exec")
            passed = True
            detail = "Python parser accepted source"
        except (SyntaxError, OSError, TypeError) as exc:
            passed = False
            detail = str(exc)
            failures.append("{}: {}".format(label, exc))
        _check(checks, "python_syntax:" + label, passed, detail)

    if tool_path:
        tool_path = os.path.realpath(os.path.abspath(tool_path))
        try:
            tool = _read(tool_path)
        except (OSError, IOError) as exc:
            tool = ""
            failures.append(str(exc))
        for pattern, label in FORBIDDEN_RUNTIME_PATTERNS:
            found = bool(re.search(pattern, tool, re.IGNORECASE))
            _check(checks, "tool_no_" + label.replace("/", "_"), not found,
                   "absent" if not found else "forbidden runtime token found")
            if found:
                failures.append("one-click tool contains {}".format(label))
        syntax_code, _stdout, syntax_error = _run(("bash", "-n", tool_path), repo_root)
        if not _check(checks, "tool_bash_syntax", syntax_code == 0,
                      syntax_error.strip() or "bash -n passed"):
            failures.append("bash syntax failed")
        marker_ok = all(marker in tool for marker in (
            "__R8_SOURCE_BUNDLE_BEGIN__", "__R8_SOURCE_BUNDLE_END__",
            "__R8_BASELINE_PERFORMANCE_BEGIN__",
            "__R8_CANDIDATE_PERFORMANCE_BEGIN__",
            "__R8_METADATA_BEGIN__", "FINAL_R8_SHA", "FINAL_R8_TREE",
        ))
        if not _check(checks, "tool_embedded_exact_inputs", marker_ok,
                      "bundle, performance, metadata markers and exact identity present"):
            failures.append("one-click embedded input markers are incomplete")

    if bundle_path:
        bundle_path = os.path.realpath(os.path.abspath(bundle_path))
        code, _stdout, error = _run(("git", "bundle", "verify", bundle_path), repo_root)
        if not _check(checks, "bundle_verify", code == 0,
                      error.strip() or "git bundle verify passed"):
            failures.append("bundle verification failed")

    result = {
        "status": "PASSED" if not failures else "FAILED",
        "evidence_class": "release_orchestration_static_validation",
        "repo_root": repo_root,
        "checks": checks,
        "violations": failures,
        "exit_code": 0 if not failures else 1,
    }
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(prog="validate_release_orchestration.py")
    parser.add_argument("--repo-root", default=os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..")
    ))
    parser.add_argument("--tool", default="")
    parser.add_argument("--bundle", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    result = validate(args.repo_root, args.tool, args.bundle)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        parent = os.path.dirname(os.path.abspath(args.output))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
    print(encoded)
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
