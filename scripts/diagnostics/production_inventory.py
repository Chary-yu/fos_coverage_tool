"""Capture fresh release-host inventory and validate the Gate F disk formula.

This tool is deliberately observation-only.  It never creates, removes, or
changes a deployment/database.  Missing production inputs stay ``INCOMPLETE``
so an old inventory or a guessed capacity cannot be promoted to Gate F proof.
"""

from __future__ import print_function

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.release_identity import generate_release_identity
from app.time_utils import utc_iso


GIB = 1024 ** 3


def required_free_bytes(current_release_bytes, candidate_release_bytes,
                        final_target_db_estimate, verified_backup_bytes,
                        max_temp_worktree_bytes, migration_temp_bytes):
    components = {
        "current_release_bytes": int(current_release_bytes),
        "candidate_release_bytes": int(candidate_release_bytes),
        "final_target_db_estimate": int(final_target_db_estimate),
        "verified_backup_bytes": int(verified_backup_bytes),
        "max_temp_worktree_bytes": int(max_temp_worktree_bytes),
        "migration_temp_bytes": int(migration_temp_bytes),
    }
    if any(value < 0 for value in components.values()):
        raise ValueError("capacity inputs must be non-negative byte counts")
    preceding_sum = sum(components.values())
    safety_margin = max(int(preceding_sum * 0.20), 10 * GIB)
    return dict(components, preceding_sum=preceding_sum,
                safety_margin_bytes=safety_margin,
                required_free_bytes=preceding_sum + safety_margin)


def _directory_bytes(path):
    total = 0
    if not path or not os.path.exists(path):
        return None
    if os.path.isfile(path):
        return os.path.getsize(path)
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [name for name in dirnames
                       if name not in (".git", "node_modules", "__pycache__")]
        for name in filenames:
            candidate = os.path.join(dirpath, name)
            try:
                total += os.path.getsize(candidate)
            except OSError:
                continue
    return total


def _path_observation(path):
    value = os.path.abspath(path) if path else ""
    exists = bool(value and os.path.exists(value))
    writable = bool(exists and os.access(value, os.W_OK))
    return {
        "path": value,
        "realpath": os.path.realpath(value) if value else "",
        "exists": exists,
        "writable": writable,
        "bytes": _directory_bytes(value) if exists else None,
    }


def _process_inventory(patterns):
    patterns = [str(item).lower() for item in (patterns or []) if str(item)]
    try:
        output = subprocess.check_output(
            ["ps", "-eo", "pid=,comm=,args="],
            stderr=subprocess.STDOUT,
        ).decode("utf-8", errors="replace")
    except (OSError, subprocess.CalledProcessError) as exc:
        return {"available": False, "error": str(exc), "matches": []}
    matches = []
    for line in output.splitlines():
        lower = line.lower()
        if patterns and not any(pattern in lower for pattern in patterns):
            continue
        matches.append(line.strip()[:2000])
    return {"available": True, "patterns": patterns, "matches": matches}


def _database_identity(config_path):
    if not config_path:
        return {"status": "INCOMPLETE", "reason": "database config is not supplied"}
    try:
        import pymysql
        from scripts.upgrade.database_identity import fingerprint_connection
        with open(config_path, "r", encoding="utf-8") as stream:
            config = json.load(stream)
        db = config.get("mysql") or config
        connection = pymysql.connect(
            host=db.get("host", "127.0.0.1"), port=int(db.get("port", 3306)),
            user=db.get("user", "root"), password=str(db.get("password", "")),
            database=db.get("database"), charset=db.get("charset", "utf8mb4"),
            connect_timeout=float(db.get("connect_timeout", 5)),
            autocommit=True,
        )
        try:
            return {"status": "PASSED", "identity": fingerprint_connection(connection, db)}
        finally:
            connection.close()
    except Exception as exc:
        return {"status": "INCOMPLETE", "reason": "{}: {}".format(type(exc).__name__, exc)}


def collect_inventory(args):
    started = utc_iso()
    current = _path_observation(args.current_root)
    candidate = _path_observation(args.candidate_root)
    root_for_disk = args.disk_root or args.current_root or os.getcwd()
    root_for_disk = os.path.abspath(root_for_disk)
    try:
        usage = shutil.disk_usage(root_for_disk)
        disk = {"path": root_for_disk, "total_bytes": usage.total,
                "used_bytes": usage.used, "free_bytes": usage.free}
    except OSError as exc:
        disk = {"path": root_for_disk, "status": "INCOMPLETE", "error": str(exc)}

    sizes = {
        "current_release_bytes": args.current_release_bytes,
        "candidate_release_bytes": args.candidate_release_bytes,
        "final_target_db_estimate": args.final_target_db_estimate,
        "verified_backup_bytes": args.verified_backup_bytes,
        "max_temp_worktree_bytes": args.max_temp_worktree_bytes,
        "migration_temp_bytes": args.migration_temp_bytes,
    }
    capacity = None
    capacity_errors = [name for name, value in sizes.items() if value is None]
    invalid_capacity = [name for name, value in sizes.items()
                        if value is not None and int(value) < 0]
    if not capacity_errors:
        if invalid_capacity:
            capacity = {"status": "INCOMPLETE",
                        "invalid_inputs": invalid_capacity}
        else:
            capacity = required_free_bytes(**sizes)
            capacity["status"] = (
                "PASSED" if disk.get("free_bytes", -1) >= capacity["required_free_bytes"]
                else "FAILED"
            )
    else:
        capacity = {"status": "INCOMPLETE",
                    "missing_inputs": capacity_errors}

    patterns = args.process_pattern or ["enhance_coverage", "coverage"]
    result = {
        "status": "PASSED",
        "evidence_class": "fresh_production_inventory",
        "synthetic": False,
        "started_at": started,
        "finished_at": utc_iso(),
        "host_identity": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "release_identity": generate_release_identity(repo_root=ROOT),
        "roots": {"current": current, "candidate": candidate},
        "disk": disk,
        "capacity_formula": capacity,
        "processes": _process_inventory(patterns),
        "database_runtime_identity": _database_identity(args.config),
        "command_or_action": "python scripts/diagnostics/production_inventory.py",
        "exit_code": 0,
        "violations": [],
    }
    if not current["exists"] or not candidate["exists"]:
        result["status"] = "INCOMPLETE"
        result["violations"].append("Current and Candidate roots must both exist")
    if capacity["status"] != "PASSED":
        result["status"] = "INCOMPLETE" if capacity["status"] == "INCOMPLETE" else "FAILED"
        result["violations"].append("fresh free-disk capacity formula is not PASSED")
    if result["database_runtime_identity"].get("status") != "PASSED":
        result["status"] = "INCOMPLETE"
        result["violations"].append("database runtime identity is unavailable")
    result["exit_code"] = 0 if result["status"] == "PASSED" else 1
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--current-root", required=True)
    parser.add_argument("--candidate-root", required=True)
    parser.add_argument("--disk-root")
    parser.add_argument("--config")
    parser.add_argument("--process-pattern", action="append")
    for name in (
            "current_release_bytes", "candidate_release_bytes",
            "final_target_db_estimate", "verified_backup_bytes",
            "max_temp_worktree_bytes", "migration_temp_bytes"):
        parser.add_argument("--{}".format(name.replace("_", "-")),
                            dest=name, type=int)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    result = collect_inventory(args)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = os.path.abspath(args.output)
        directory = os.path.dirname(output)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(output, "w", encoding="utf-8") as stream:
            stream.write(encoded)
    print(encoded)
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
