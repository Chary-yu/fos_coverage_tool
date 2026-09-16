#!/usr/bin/env python3
"""Run deterministic Python 3.6/MariaDB 5.5 compatibility checks locally.

The image references are copied from the former compatibility lane and are
required to be digest-pinned.  No network is enabled for containers.  If a
Docker daemon is unavailable, this gate reports NOT_VERIFIED and never turns
that absence into a false PASS.
"""

from __future__ import print_function

import argparse
import json
import os
import shutil
import subprocess
import time


PYTHON36_IMAGE = (
    "python@sha256:f8652afaf88c25f0d22354d547d892591067aa4026a7fa9a6819df9f300af6fc"
)
MARIADB55_IMAGE = (
    "mariadb@sha256:8665c074af5a5fb7e04b9570fcf8551e9d82955182be50375d5013838d4f9137"
)
_RELEASE_IMPORTS = (
    "app.release_trust_policy",
    "scripts.release.release_input_manifest",
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
        completed = subprocess.Popen(
            list(argv), cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        stdout, _ = completed.communicate(timeout=timeout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return completed.returncode, stdout.decode("utf-8", "replace")[-12000:]


def _write(path, payload):
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    temporary = "{}.tmp-{}".format(path, os.getpid())
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    os.replace(temporary, path)


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
        "image": PYTHON36_IMAGE,
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
            "status": "FAIL", "compatibility": "FAIL", "image": MARIADB55_IMAGE,
            "command": run_command, "exit_code": code,
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
                "image": MARIADB55_IMAGE, "command": run_command,
                "exit_code": 1, "output_tail": last_output,
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
                    "image": MARIADB55_IMAGE, "command": run_command,
                    "exit_code": code, "output_tail": output,
                }
        return {
            "status": "PASS", "compatibility": "PASS", "image": MARIADB55_IMAGE,
            "command": run_command + ["(grant/revoke/cleanup)"],
            "exit_code": 0, "output_tail": "grant/revoke/cleanup passed",
        }
    finally:
        _run([docker, "rm", "-f", container], timeout=30)


def run_gate(repo_root, output_path):
    """Run the compatibility gate and write one machine-readable record."""
    repo_root = os.path.abspath(repo_root)
    docker = shutil.which("docker")
    payload = {
        "schema_version": 1,
        "status": "NOT_VERIFIED",
        "PYTHON36_COMPAT": "NOT_VERIFIED",
        "MARIADB55_COMPAT": "NOT_VERIFIED",
        "docker_available": bool(docker),
        "python36_image": PYTHON36_IMAGE,
        "mariadb55_image": MARIADB55_IMAGE,
        "network_access": "NONE",
        "production_mutation": "NONE",
        "repo_root": repo_root,
    }
    if not docker:
        payload["blocker"] = "docker executable/daemon is unavailable"
        _write(output_path, payload)
        return payload
    docker_code, docker_output = _run([docker, "info", "--format", "{{.ServerVersion}}"],
                                      timeout=30)
    if docker_code != 0:
        payload["blocker"] = "docker daemon is unavailable"
        payload["docker_info"] = docker_output
        _write(output_path, payload)
        return payload
    python_result = _python36_check(repo_root, docker)
    mariadb_result = _mariadb55_check(docker)
    payload.update({
        "python36": python_result,
        "mariadb55": mariadb_result,
        "PYTHON36_COMPAT": python_result["compatibility"],
        "MARIADB55_COMPAT": mariadb_result["compatibility"],
    })
    passed = python_result["compatibility"] == "PASS" and \
        mariadb_result["compatibility"] == "PASS"
    payload["status"] = "PASS" if passed else "FAIL"
    _write(output_path, payload)
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(prog="local_compatibility_gate.py")
    parser.add_argument("--repo-root", default=os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../..")
    ))
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    result = run_gate(args.repo_root, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
