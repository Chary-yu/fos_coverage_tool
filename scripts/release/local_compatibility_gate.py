#!/usr/bin/env python3
"""Run deterministic Python 3.6/MariaDB 5.5 compatibility checks locally.

The compatibility surface classifier decides whether each legacy runtime is
relevant to this exact source/baseline pair.  Each relevant surface then has
one truthful state: PASS, NOT_APPLICABLE, DEFERRED_TO_TARGET_RUNTIME, or
FAIL.  Deferred compatibility is sufficient for isolated Candidate staging
only; it never becomes publication or cutover authority.
"""

from __future__ import print_function

import argparse
import json
import os
import shutil
import subprocess
import time

from scripts.release.compatibility_surface import (
    classify_compatibility_surface,
)


PYTHON36_IMAGE = (
    "python@sha256:f8652afaf88c25f0d22354d547d892591067aa4026a7fa9a6819df9f300af6fc"
)
MARIADB55_IMAGE = (
    "mariadb@sha256:8665c074af5a5fb7e04b9570fcf8551e9d82955182be50375d5013838d4f9137"
)
COMPATIBILITY_STATUSES = (
    "PASS", "NOT_APPLICABLE", "DEFERRED_TO_TARGET_RUNTIME", "FAIL",
)
_PYTHON_FILES = (
    "app/release_trust_policy.py",
    "app/candidate_artifact.py",
    "app/release_publication.py",
    "scripts/release/release_input_manifest.py",
    "scripts/release/flat_candidate_operator_runtime.py",
    "scripts/release/build_production_candidate_artifact.py",
    "scripts/release/build_flat_production_candidate_artifact.py",
)


def _run(argv, cwd=None, timeout=120):
    try:
        process = subprocess.Popen(
            list(argv), cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        stdout, _ = process.communicate(timeout=timeout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return process.returncode, stdout.decode("utf-8", "replace")[-12000:]


def _write(path, payload):
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    temporary = "{}.tmp-{}".format(path, os.getpid())
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    os.replace(temporary, path)


def _head_sha(repo_root):
    code, output = _run(["git", "-C", repo_root, "rev-parse", "HEAD"])
    if code != 0:
        raise ValueError("cannot determine repository HEAD: {}".format(output))
    return output.strip().lower()


def _python36_check(repo_root, docker):
    command = [docker, "run", "--rm", "--network", "none",
               "-v", "{}:/workspace:ro".format(os.path.abspath(repo_root)),
               "-w", "/workspace", PYTHON36_IMAGE, "python", "-c"]
    code = (
        "import py_compile\n"
        "files = {}\n"
        "[py_compile.compile(path, doraise=True) for path in files]\n"
        "import app.release_trust_policy\n"
        "import scripts.release.release_input_manifest\n"
    ).format(repr(list(_PYTHON_FILES)))
    command.append(code)
    exit_code, output = _run(command, timeout=180)
    return {
        "status": "PASS" if exit_code == 0 else "FAIL",
        "compatibility": "PASS" if exit_code == 0 else "FAIL",
        "runtime": "docker",
        "image": PYTHON36_IMAGE,
        "command": command,
        "exit_code": exit_code,
        "output_tail": output,
    }


def _python36_check_local(repo_root, python36):
    command = [python36, "-c"]
    code = (
        "import py_compile\n"
        "files = {}\n"
        "[py_compile.compile(path, doraise=True) for path in files]\n"
        "import app.release_trust_policy\n"
        "import scripts.release.release_input_manifest\n"
    ).format(repr(list(_PYTHON_FILES)))
    command.append(code)
    exit_code, output = _run(command, cwd=repo_root, timeout=180)
    return {
        "status": "PASS" if exit_code == 0 else "FAIL",
        "compatibility": "PASS" if exit_code == 0 else "FAIL",
        "runtime": "local_python36",
        "command": command,
        "exit_code": exit_code,
        "output_tail": output,
    }


def _mariadb_exec(docker, container, client, sql):
    return _run([
        docker, "exec", container, client, "-uroot", "-plocal-only",
        "-e", sql,
    ], timeout=30)


def _mariadb55_check(docker):
    run_command = [
        docker, "run", "-d", "--network", "none",
        "-e", "MARIADB_ROOT_PASSWORD=local-only",
        "-e", "MYSQL_ROOT_PASSWORD=local-only",
        MARIADB55_IMAGE,
    ]
    code, output = _run(run_command, timeout=60)
    container = output.strip().splitlines()[-1] if output.strip() else ""
    if code != 0 or not container:
        return {
            "status": "FAIL", "compatibility": "FAIL", "runtime": "docker",
            "image": MARIADB55_IMAGE, "command": run_command, "exit_code": code,
            "output_tail": output,
        }
    try:
        ready = False
        last_output = ""
        for _attempt in range(60):
            client = "mysql"
            probe_code, probe_output = _mariadb_exec(
                docker, container, client, "SELECT 1"
            )
            if probe_code != 0:
                client = "mariadb"
                probe_code, probe_output = _mariadb_exec(
                    docker, container, client, "SELECT 1"
                )
            last_output = probe_output
            if probe_code == 0:
                ready = True
                break
            time.sleep(1)
        if not ready:
            return {
                "status": "FAIL", "compatibility": "FAIL",
                "runtime": "docker", "image": MARIADB55_IMAGE,
                "command": run_command, "exit_code": 1,
                "output_tail": last_output,
            }
        statements = (
            "CREATE DATABASE IF NOT EXISTS fos_compat;",
            "CREATE USER IF NOT EXISTS 'fos_gate'@'localhost' IDENTIFIED BY 'local-only';",
            "GRANT SELECT, INSERT ON fos_compat.* TO 'fos_gate'@'localhost';",
            "REVOKE INSERT ON fos_compat.* FROM 'fos_gate'@'localhost';",
            "DROP USER 'fos_gate'@'localhost';",
            "DROP DATABASE fos_compat;",
        )
        for statement in statements:
            code, output = _mariadb_exec(docker, container, client, statement)
            if code != 0:
                return {
                    "status": "FAIL", "compatibility": "FAIL",
                    "runtime": "docker", "image": MARIADB55_IMAGE,
                    "command": run_command, "exit_code": code,
                    "output_tail": output,
                }
        return {
            "status": "PASS", "compatibility": "PASS", "runtime": "docker",
            "image": MARIADB55_IMAGE,
            "command": run_command + ["(grant/revoke/cleanup)"],
            "exit_code": 0, "output_tail": "grant/revoke/cleanup passed",
        }
    finally:
        _run([docker, "rm", "-f", container], timeout=30)


def _not_applicable(surface, reason):
    return {
        "status": "NOT_APPLICABLE",
        "compatibility": "NOT_APPLICABLE",
        "surface": surface,
        "reason": reason,
        "exit_code": 0,
    }


def _deferred(surface, reason, runtime=None):
    result = {
        "status": "DEFERRED_TO_TARGET_RUNTIME",
        "compatibility": "DEFERRED_TO_TARGET_RUNTIME",
        "surface": surface,
        "reason": reason,
        "exit_code": 0,
    }
    if runtime:
        result["required_runtime"] = runtime
    return result


def _overall_status(statuses):
    if "FAIL" in statuses:
        return "FAIL"
    if "DEFERRED_TO_TARGET_RUNTIME" in statuses:
        return "DEFERRED_TO_TARGET_RUNTIME"
    if statuses and all(status == "NOT_APPLICABLE" for status in statuses):
        return "NOT_APPLICABLE"
    return "PASS"


def _surface_status(surface, key):
    return str((surface.get(key) or {}).get("status") or "")


def run_gate(repo_root, output_path, source_sha=None, baseline_sha=None):
    """Run the compatibility gate and write one machine-readable record."""
    repo_root = os.path.abspath(repo_root)
    source_sha = str(source_sha or _head_sha(repo_root)).strip().lower()
    baseline_sha = str(baseline_sha or source_sha).strip().lower()
    try:
        surface = classify_compatibility_surface(
            repo_root, source_sha, baseline_sha
        )
    except (OSError, ValueError, TypeError) as exc:
        payload = {
            "schema_version": 1,
            "status": "FAIL",
            "PYTHON36_COMPAT": "FAIL",
            "MARIADB55_COMPAT": "FAIL",
            "source_commit_sha": source_sha,
            "baseline_commit_sha": baseline_sha,
            "compatibility_surface_error": str(exc),
            "network_access": "NONE",
            "production_mutation": "NONE",
            "isolated_candidate_staging_allowed": False,
            "publication_allowed": False,
            "cutover_allowed": False,
        }
        _write(output_path, payload)
        return payload

    docker = shutil.which("docker")
    python36 = shutil.which("python3.6")
    docker_info = ""
    docker_daemon_available = False
    if docker:
        docker_code, docker_info = _run(
            [docker, "info", "--format", "{{.ServerVersion}}"], timeout=30
        )
        docker_daemon_available = docker_code == 0

    payload = {
        "schema_version": 1,
        "status": "NOT_APPLICABLE",
        "PYTHON36_COMPAT": "NOT_APPLICABLE",
        "MARIADB55_COMPAT": "NOT_APPLICABLE",
        "source_commit_sha": source_sha,
        "baseline_commit_sha": baseline_sha,
        "compatibility_surface": surface,
        "docker_available": bool(docker),
        "docker_daemon_available": docker_daemon_available,
        "python36_available": bool(python36),
        "python36_image": PYTHON36_IMAGE,
        "mariadb55_image": MARIADB55_IMAGE,
        "network_access": "NONE",
        "production_mutation": "NONE",
    }
    if docker and not docker_daemon_available:
        payload["docker_info"] = docker_info

    python_changed = _surface_status(surface, "python") == \
        "PRODUCTION_PYTHON_CHANGED"
    if not python_changed:
        python_result = _not_applicable(
            "python",
            "no production-executed Python file changed",
        )
    elif docker_daemon_available:
        python_result = _python36_check(repo_root, docker)
    elif python36:
        python_result = _python36_check_local(repo_root, python36)
    else:
        python_result = _deferred(
            "python",
            "production-executed Python changed but Python 3.6 is unavailable locally",
            "Python 3.6 on VFOS target runtime",
        )

    mariadb_changed = _surface_status(surface, "mariadb") == \
        "DB_SENSITIVE_CHANGED"
    if not mariadb_changed:
        mariadb_result = _not_applicable(
            "mariadb",
            "no DB-sensitive surface changed",
        )
    elif docker_daemon_available:
        mariadb_result = _mariadb55_check(docker)
    else:
        mariadb_result = _deferred(
            "mariadb",
            "DB-sensitive surface changed but MariaDB 5.5 runtime is unavailable locally",
            "MariaDB 5.5 on the target compatibility runtime",
        )

    python_status = python_result["status"]
    mariadb_status = mariadb_result["status"]
    overall = _overall_status((python_status, mariadb_status))
    payload.update({
        "python36": python_result,
        "mariadb55": mariadb_result,
        "PYTHON36_COMPAT": python_status,
        "MARIADB55_COMPAT": mariadb_status,
        "status": overall,
        "isolated_candidate_staging_allowed": overall != "FAIL",
        "publication_allowed": overall in ("PASS", "NOT_APPLICABLE"),
        "cutover_allowed": overall in ("PASS", "NOT_APPLICABLE"),
        "deferred_surfaces": [
            name for name, status in (
                ("python", python_status), ("mariadb", mariadb_status)
            ) if status == "DEFERRED_TO_TARGET_RUNTIME"
        ],
    })
    _write(output_path, payload)
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(prog="local_compatibility_gate.py")
    parser.add_argument("--repo-root", default=os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../..")
    ))
    parser.add_argument("--source-sha", default="")
    parser.add_argument("--baseline-sha", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    result = run_gate(
        args.repo_root, args.output,
        source_sha=args.source_sha or None,
        baseline_sha=args.baseline_sha or None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] in (
        "PASS", "NOT_APPLICABLE", "DEFERRED_TO_TARGET_RUNTIME"
    ) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
