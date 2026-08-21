"""Capture fresh release-host inventory and validate the Gate F disk formula.

This tool is deliberately observation-only.  It never creates, removes, or
changes a deployment/database.  Missing production inputs stay ``INCOMPLETE``
so an old inventory or a guessed capacity cannot be promoted to Gate F proof.
"""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.release_identity import generate_release_identity
from app.time_utils import utc_iso


GIB = 1024 ** 3

_INVENTORY_TABLES = (
    "coverage_schema_meta", "coverage_schema_migrations",
    "coverage_projects", "coverage_scans", "coverage_scan_repositories",
    "coverage_reports", "coverage_files", "coverage_lines",
    "coverage_analyses", "coverage_project_state", "coverage_file_state",
    "coverage_background_jobs", "coverage_incremental_results",
    "coverage_legacy_provenance", "coverage_repositories",
    "coverage_repository_aliases", "coverage_repository_resources",
    "coverage_analysis_records", "coverage_analysis_blocks",
    "coverage_inheritance_groups", "coverage_analysis_line_links",
    "coverage_inheritance_decisions", "coverage_inheritance_rejections",
    "coverage_repository_resource_locks", "coverage_import_artifacts",
    "coverage_import_checkpoints", "coverage_import_failures",
)

_ACTIVE_JOB_STATES = ("queued", "running", "retrying", "interrupted")


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


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repo_observation(path):
    """Capture a read-only Git identity for one deployed repository root."""
    observed = _path_observation(path)
    result = {"path": observed["path"], "realpath": observed["realpath"],
              "exists": observed["exists"], "status": "INCOMPLETE",
              "head": "", "git_common_dir": "", "clean": False,
              "violations": []}
    if not observed["exists"]:
        result["violations"].append("repository root does not exist")
        return result
    try:
        result["top_level"] = subprocess.check_output(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            stderr=subprocess.STDOUT,
        ).decode("utf-8", errors="replace").strip()
        result["head"] = subprocess.check_output(
            ["git", "-C", path, "rev-parse", "HEAD"],
            stderr=subprocess.STDOUT,
        ).decode("ascii").strip()
        result["git_common_dir"] = subprocess.check_output(
            ["git", "-C", path, "rev-parse", "--git-common-dir"],
            stderr=subprocess.STDOUT,
        ).decode("utf-8", errors="replace").strip()
        dirty = subprocess.check_output(
            ["git", "-C", path, "status", "--porcelain", "--untracked-files=all"],
            stderr=subprocess.STDOUT,
        ).decode("utf-8", errors="replace")
        result["clean"] = not bool(dirty.strip())
        result["status"] = "PASSED" if result["head"] and result["clean"] else "INCOMPLETE"
        if not result["head"]:
            result["violations"].append("repository HEAD is unavailable")
        if not result["clean"]:
            result["violations"].append("repository worktree is dirty")
    except (OSError, subprocess.CalledProcessError, UnicodeError) as exc:
        result["violations"].append("git identity probe failed: {}".format(exc))
    return result


def _process_inventory(patterns, expected_pids=None):
    patterns = [str(item).lower() for item in (patterns or []) if str(item)]
    normalized_pids = set()
    for item in expected_pids or []:
        try:
            value = int(item)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            normalized_pids.add(value)
    expected_pids = normalized_pids
    try:
        output = subprocess.check_output(
            ["ps", "-eo", "pid=,comm=,args="],
            stderr=subprocess.STDOUT,
        ).decode("utf-8", errors="replace")
    except (OSError, subprocess.CalledProcessError) as exc:
        return {"available": False, "error": str(exc), "matches": [],
                "matched_pids": [], "service_pid_matches": []}
    matches = []
    matched_pids = []
    for line in output.splitlines():
        lower = line.lower()
        if patterns and not any(pattern in lower for pattern in patterns):
            continue
        cleaned = line.strip()
        matches.append(cleaned[:2000])
        try:
            pid = int(cleaned.split(None, 1)[0])
        except (IndexError, TypeError, ValueError):
            continue
        matched_pids.append(pid)
    return {
        "available": True, "patterns": patterns, "matches": matches,
        "matched_pids": sorted(set(matched_pids)),
        "service_pid_matches": sorted(set(matched_pids).intersection(expected_pids)),
    }


def _service_inventory(services):
    results = []
    main_pids = []
    for service in services or []:
        try:
            output = subprocess.check_output(
                ["systemctl", "show", str(service),
                 "--property=ActiveState,SubState,MainPID,FragmentPath"],
                stderr=subprocess.STDOUT,
            ).decode("utf-8", errors="replace")
            values = dict(
                line.split("=", 1) for line in output.splitlines() if "=" in line
            )
            results.append({
                "service": str(service), "available": True,
                "active": values.get("ActiveState") == "active",
                "properties": values,
            })
            try:
                pid = int(values.get("MainPID") or 0)
            except (TypeError, ValueError):
                pid = 0
            if pid > 0:
                main_pids.append(pid)
        except (OSError, subprocess.CalledProcessError) as exc:
            results.append({"service": str(service), "available": False,
                            "active": False, "error": str(exc)})
    return {
        "requested": [str(item) for item in (services or [])],
        "available": bool(results) and all(item.get("available") for item in results),
        "active": bool(results) and all(item.get("active") for item in results),
        "main_pids": sorted(set(main_pids)),
        "items": results,
    }


def _listening_ports():
    ports = set()
    for template in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(template, "r", encoding="ascii") as stream:
                for line in stream.read().splitlines()[1:]:
                    fields = line.split()
                    if len(fields) < 4 or fields[3] != "0A":
                        continue
                    ports.add(int(fields[1].rsplit(":", 1)[-1], 16))
        except (OSError, ValueError, IndexError):
            continue
    return sorted(ports)


def _load_config(path):
    if not path:
        return {"status": "INCOMPLETE", "path": "", "config": {},
                "violations": ["runtime config path is not supplied"]}
    try:
        with open(path, "r", encoding="utf-8") as stream:
            config = json.load(stream)
        if not isinstance(config, dict):
            raise ValueError("runtime config must be an object")
        return {"status": "PASSED", "path": os.path.abspath(path),
                "config": config, "violations": []}
    except (OSError, ValueError, TypeError) as exc:
        return {"status": "INCOMPLETE", "path": os.path.abspath(path),
                "config": {}, "violations": ["config load failed: {}".format(exc)]}


def _redacted_config(config):
    """Return only non-secret configuration fields useful for inventory proof."""
    mysql = config.get("mysql") or {}
    server = config.get("server") or {}
    auth = config.get("auth") or {}
    state = config.get("runtime_state") or {}
    return {
        "runtime_mode": config.get("runtime_mode"),
        "schema_version": config.get("schema_version"),
        "environment": config.get("environment"),
        "mysql": {
            "host": mysql.get("host"), "port": mysql.get("port"),
            "user": mysql.get("user"), "database": mysql.get("database"),
        },
        "server": {"host": server.get("host"), "port": server.get("port")},
        "auth": {
            "mode": auth.get("mode"),
            "user_header": auth.get("user_header"),
            "trusted_proxy_addresses": list(auth.get("trusted_proxy_addresses") or []),
            "allowed_origins": list(auth.get("allowed_origins") or []),
        },
        "runtime_state": {
            "root": state.get("root"), "jobs_dir": state.get("jobs_dir"),
            "registry_dir": state.get("registry_dir"),
            "exports_dir": state.get("exports_dir"),
        },
        "input_roots": list(config.get("input_roots") or []),
        "report_roots": list(config.get("report_roots") or []),
    }


def _config_observation(path, role, root):
    loaded = _load_config(path)
    result = {
        "role": role, "path": loaded.get("path") or "",
        "status": loaded.get("status", "INCOMPLETE"),
        "sha256": "", "config": {}, "violations": list(loaded.get("violations") or []),
    }
    if result["status"] != "PASSED":
        return result
    absolute = loaded["path"]
    try:
        result["sha256"] = _sha256_file(absolute)
    except (OSError, IOError) as exc:
        result["status"] = "INCOMPLETE"
        result["violations"].append("config hash failed: {}".format(exc))
        return result
    config = loaded["config"]
    result["config"] = _redacted_config(config)
    base_root = os.path.abspath(root or os.path.dirname(absolute))
    state = config.get("runtime_state") or {}
    result["resolved_runtime_paths"] = {
        "root": _resolve_path(state.get("root"), base_root),
        "jobs_dir": _resolve_path(
            state.get("jobs_dir"), _resolve_path(state.get("root"), base_root)
        ),
        "registry_dir": _resolve_path(
            state.get("registry_dir"), _resolve_path(state.get("root"), base_root)
        ),
        "exports_dir": _resolve_path(
            state.get("exports_dir"), _resolve_path(state.get("root"), base_root)
        ),
        "input_roots": [
            _resolve_path(item, base_root) for item in (config.get("input_roots") or [])
        ],
        "report_roots": [
            _resolve_path(item, base_root) for item in (config.get("report_roots") or [])
        ],
    }
    return result


def _resolve_path(value, base):
    if not value:
        return ""
    value = os.path.expandvars(os.path.expanduser(str(value)))
    if not os.path.isabs(value):
        value = os.path.join(base, value)
    return os.path.abspath(value)


def _database_identity(config_path):
    """Read DB identity, schema versions, table counts and job/data versions."""
    empty = {
        "status": "INCOMPLETE", "identity": {}, "schema": [],
        "schema_query": False, "table_counts": {}, "table_count_query": False,
        "data_versions": [], "data_version_query": False,
        "jobs": {"by_state": {}, "total": 0, "active_count": 0},
        "job_query": False, "violations": [],
    }
    if not config_path:
        empty["violations"].append("database config is not supplied")
        return empty
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
            autocommit=False, cursorclass=pymysql.cursors.DictCursor,
        )
    except Exception as exc:
        empty["violations"].append("{}: {}".format(type(exc).__name__, exc))
        return empty

    errors = []
    result = dict(empty)
    try:
        result["identity"] = fingerprint_connection(connection, db)
        with connection.cursor() as cursor:
            try:
                cursor.execute(
                    "SELECT schema_key, schema_version, release_sha, migration_id "
                    "FROM coverage_schema_meta ORDER BY schema_key"
                )
                result["schema"] = [dict(item) for item in (cursor.fetchall() or [])]
                result["schema_query"] = True
            except Exception as exc:
                errors.append("schema query failed: {}".format(exc))

            for table in _INVENTORY_TABLES:
                try:
                    cursor.execute("SELECT COUNT(*) AS total FROM `{}`".format(table))
                    result["table_counts"][table] = int(
                        (cursor.fetchone() or {}).get("total") or 0
                    )
                except Exception as exc:
                    errors.append("{} count failed: {}".format(table, exc))
            result["table_count_query"] = len(result["table_counts"]) == len(_INVENTORY_TABLES)

            try:
                cursor.execute(
                    "SELECT p.project_name, s.project_id, s.data_version, "
                    "s.file_state_version, s.current_scan_id "
                    "FROM coverage_project_state s "
                    "LEFT JOIN coverage_projects p ON p.id = s.project_id "
                    "ORDER BY p.project_name, s.project_id"
                )
                result["data_versions"] = [
                    dict(item) for item in (cursor.fetchall() or [])
                ]
                result["data_version_query"] = True
            except Exception as exc:
                errors.append("data_version query failed: {}".format(exc))

            try:
                cursor.execute(
                    "SELECT state, COUNT(*) AS total "
                    "FROM coverage_background_jobs GROUP BY state ORDER BY state"
                )
                by_state = {
                    str(item.get("state")): int(item.get("total") or 0)
                    for item in (cursor.fetchall() or [])
                }
                result["jobs"] = {
                    "by_state": by_state,
                    "total": sum(by_state.values()),
                    "active_count": sum(
                        by_state.get(state, 0) for state in _ACTIVE_JOB_STATES
                    ),
                }
                result["job_query"] = True
            except Exception as exc:
                errors.append("job state query failed: {}".format(exc))
    except Exception as exc:
        errors.append("database inventory query failed: {}".format(exc))
    finally:
        try:
            connection.rollback()
        except Exception:
            pass
        connection.close()

    result["violations"] = errors
    result["status"] = (
        "PASSED" if result["identity"].get("probe_status") == "PASSED"
        and result["schema_query"] and bool(result["schema"])
        and result["table_count_query"] and result["data_version_query"]
        and result["job_query"] and not errors else "INCOMPLETE"
    )
    return result


def _persistent_observations(configs, explicit_roots, jobs_roots):
    candidates = []
    for item in explicit_roots or []:
        candidates.append((str(item), "explicit"))
    for item in jobs_roots or []:
        candidates.append((str(item), "jobs"))
    for role, observation in configs.items():
        for key, path in (observation.get("resolved_runtime_paths") or {}).items():
            if isinstance(path, list):
                for index, item in enumerate(path):
                    candidates.append((item, "{}[{}]".format(key, index)))
            elif path:
                candidates.append((path, key))
    seen = set()
    results = []
    for path, purpose in candidates:
        absolute = os.path.abspath(path) if path else ""
        realpath = os.path.realpath(absolute) if absolute else ""
        key = (realpath, purpose)
        if not absolute or key in seen:
            continue
        seen.add(key)
        observed = _path_observation(absolute)
        observed["purpose"] = purpose
        results.append(observed)
    return results


def _backup_observation(path, deployment_roots):
    observed = _path_observation(path)
    deployment_realpaths = [
        item.get("realpath") for item in deployment_roots
        if item.get("realpath")
    ]
    backup_realpath = observed.get("realpath") or ""
    inside = any(
        backup_realpath == root or backup_realpath.startswith(root + os.sep)
        for root in deployment_realpaths
    )
    observed["external_to_deployment_roots"] = bool(
        observed.get("exists") and not inside
    )
    observed["status"] = (
        "PASSED" if observed.get("external_to_deployment_roots") else "INCOMPLETE"
    )
    if not observed.get("exists"):
        observed["violation"] = "backup root does not exist"
    elif inside:
        observed["violation"] = "backup root is inside a deployment root"
    return observed


def _proxy_observation(paths, expected_header="X-Remote-User"):
    items = []
    contents = []
    for path in paths or []:
        observed = _path_observation(path)
        item = dict(observed, readable=False, sha256="", signals={})
        if observed.get("exists") and os.path.isfile(observed.get("path")):
            try:
                with open(observed["path"], "r", encoding="utf-8", errors="replace") as stream:
                    content = stream.read()
                item["readable"] = True
                item["sha256"] = _sha256_file(observed["path"])
                item["signals"] = {
                    "proxy_pass": bool(re.search(r"\bproxy_pass\b", content)),
                    "auth_request": bool(re.search(r"\bauth_request\b", content)),
                    "auth_basic": bool(re.search(r"\bauth_basic\b", content)),
                    "remote_user_header": bool(re.search(
                        r"\bproxy_set_header\s+{}\b".format(re.escape(expected_header)),
                        content, re.IGNORECASE,
                    )),
                    "trusted_source_directives": re.findall(
                        r"\b(?:set_real_ip_from|allow)\s+([^;\s]+)",
                        content, re.IGNORECASE,
                    ),
                }
                contents.append(content)
            except (OSError, IOError, UnicodeError) as exc:
                item["error"] = "proxy config read failed: {}".format(exc)
        else:
            item["error"] = "proxy config does not exist or is not a file"
        items.append(item)
    combined = "\n".join(contents)
    signals = {
        "proxy_pass": bool(re.search(r"\bproxy_pass\b", combined)),
        "auth_request": bool(re.search(r"\bauth_request\b", combined)),
        "auth_basic": bool(re.search(r"\bauth_basic\b", combined)),
        "remote_user_header": bool(re.search(
            r"\bproxy_set_header\s+{}\b".format(re.escape(expected_header)),
            combined, re.IGNORECASE,
        )),
    }
    readable = bool(items) and all(item.get("readable") for item in items)
    status = "PASSED" if readable and signals["remote_user_header"] and (
        signals["auth_request"] or signals["auth_basic"]
    ) else "INCOMPLETE"
    return {
        "status": status, "requested": [str(item) for item in (paths or [])],
        "expected_user_header": expected_header, "items": items, "signals": signals,
        "violations": [] if status == "PASSED" else [
            "proxy/auth config is missing, unreadable, or lacks an explicit auth boundary"
        ],
    }


def _auth_boundary(configs, proxy):
    candidate = configs.get("candidate") or {}
    config = candidate.get("config") or {}
    auth = config.get("auth") or {}
    expected_header = str(auth.get("user_header") or "")
    violations = []
    if candidate.get("status") != "PASSED":
        violations.append("Candidate config is unavailable")
    if str(auth.get("mode") or "").lower() != "reverse_proxy":
        violations.append("Candidate auth mode is not reverse_proxy")
    if not expected_header:
        violations.append("Candidate auth user_header is missing")
    if not auth.get("trusted_proxy_addresses"):
        violations.append("Candidate trusted_proxy_addresses are missing")
    if proxy.get("status") != "PASSED":
        violations.extend(proxy.get("violations") or ["proxy evidence is incomplete"])
    elif not proxy.get("signals", {}).get("remote_user_header"):
        violations.append("proxy does not explicitly set the configured user header")
    return {
        "status": "PASSED" if not violations else "INCOMPLETE",
        "mode": auth.get("mode"), "user_header": expected_header,
        "trusted_proxy_addresses": list(auth.get("trusted_proxy_addresses") or []),
        "allowed_origins": list(auth.get("allowed_origins") or []),
        "proxy_status": proxy.get("status"), "violations": violations,
    }


def _repository_observations(args, current, candidate):
    paths = list(getattr(args, "repository_root", None) or [])
    if getattr(args, "current_repository_root", None):
        paths.append(args.current_repository_root)
    if getattr(args, "candidate_repository_root", None):
        paths.append(args.candidate_repository_root)
    if not paths:
        paths = [current.get("path"), candidate.get("path")]
    results = []
    seen = set()
    for path in paths:
        absolute = os.path.abspath(path) if path else ""
        if not absolute or absolute in seen:
            continue
        seen.add(absolute)
        results.append(_repo_observation(absolute))
    return results


def _port_observation(configs, explicit_ports):
    expected = []
    for role in ("current", "candidate"):
        config = (configs.get(role) or {}).get("config") or {}
        port = (config.get("server") or {}).get("port")
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = 0
        if port > 0:
            expected.append({"role": role, "port": port})
    for port in explicit_ports or []:
        try:
            value = int(port)
        except (TypeError, ValueError):
            value = 0
        if value > 0 and not any(item["port"] == value for item in expected):
            expected.append({"role": "explicit", "port": value})
    observed = _listening_ports()
    missing = [item for item in expected if item["port"] not in observed]
    return {
        "status": "PASSED" if expected and not missing else "INCOMPLETE",
        "expected": expected, "observed_listening_ports": observed,
        "missing": missing,
        "violations": [] if expected and not missing else [
            "configured service ports are not fully observed as listening"
        ],
    }


def _release_identity_for_root(path):
    if path and os.path.exists(os.path.join(path, ".git")):
        return generate_release_identity(repo_root=path)
    return {}


def collect_inventory(args):
    started = utc_iso()
    current = _path_observation(args.current_root)
    candidate = _path_observation(args.candidate_root)
    configs = {
        "current": _config_observation(
            getattr(args, "current_config", None), "current", current.get("path")
        ),
        "candidate": _config_observation(
            getattr(args, "candidate_config", None), "candidate", candidate.get("path")
        ),
    }
    repo_observations = _repository_observations(args, current, candidate)
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
    capacity_errors = [name for name, value in sizes.items() if value is None]
    invalid_capacity = [name for name, value in sizes.items()
                        if value is not None and int(value) < 0]
    if capacity_errors:
        capacity = {"status": "INCOMPLETE", "missing_inputs": capacity_errors}
    elif invalid_capacity:
        capacity = {"status": "INCOMPLETE", "invalid_inputs": invalid_capacity}
    else:
        capacity = required_free_bytes(**sizes)
        capacity["status"] = (
            "PASSED" if disk.get("free_bytes", -1) >= capacity["required_free_bytes"]
            else "FAILED"
        )

    services = _service_inventory(getattr(args, "service", None) or [])
    patterns = list(getattr(args, "process_pattern", None) or [])
    processes = _process_inventory(patterns, services.get("main_pids"))
    ports = _port_observation(configs, getattr(args, "port", None) or [])
    current_db = _database_identity(configs["current"].get("path")) \
        if configs["current"].get("status") == "PASSED" else _database_identity(None)
    candidate_db = _database_identity(configs["candidate"].get("path")) \
        if configs["candidate"].get("status") == "PASSED" else _database_identity(None)
    db_inventory = {
        "status": "PASSED" if current_db.get("status") == "PASSED"
        and candidate_db.get("status") == "PASSED" else "INCOMPLETE",
        "current": current_db, "candidate": candidate_db,
    }
    explicit_jobs_roots = list(getattr(args, "jobs_root", None) or [])
    persistent_roots = _persistent_observations(
        configs, getattr(args, "persistent_root", None) or [], explicit_jobs_roots
    )
    jobs_roots = [item for item in persistent_roots if item.get("purpose") == "jobs"
                  or item.get("purpose") == "jobs_dir"]
    if not jobs_roots:
        jobs_roots = [item for item in persistent_roots
                      if str(item.get("purpose") or "").startswith("jobs")]
    jobs = {
        "status": "PASSED" if jobs_roots and all(item.get("exists") for item in jobs_roots)
        and current_db.get("job_query") and candidate_db.get("job_query") else "INCOMPLETE",
        "filesystem": jobs_roots,
        "current_database": current_db.get("jobs") or {},
        "candidate_database": candidate_db.get("jobs") or {},
    }
    backup = _backup_observation(
        getattr(args, "backup_root", None), [current, candidate]
    )
    expected_header = str(
        ((configs.get("candidate") or {}).get("config") or {}).get("auth", {}).get(
            "user_header", "X-Remote-User"
        ) or "X-Remote-User"
    )
    proxy = _proxy_observation(getattr(args, "proxy_config", None) or [], expected_header)
    auth_boundary = _auth_boundary(configs, proxy)

    persistent_ok = bool(persistent_roots) and all(
        item.get("exists") and item.get("writable") for item in persistent_roots
    )
    checks = {
        "dual_environment_roots": bool(
            current.get("exists") and candidate.get("exists")
            and current.get("realpath") != candidate.get("realpath")
        ),
        "repository_heads": len(repo_observations) >= 2
        and len(set(item.get("realpath") for item in repo_observations)) >= 2
        and all(
            item.get("status") == "PASSED" for item in repo_observations
        ),
        "service_process_inventory": bool(
            patterns and processes.get("available") and processes.get("matches")
            and processes.get("service_pid_matches")
            and services.get("requested") and services.get("available")
            and services.get("active")
        ),
        "configured_ports": ports.get("status") == "PASSED",
        "current_db_identity": current_db.get("status") == "PASSED",
        "candidate_db_identity": candidate_db.get("status") == "PASSED",
        "schema_table_counts": current_db.get("table_count_query")
        and candidate_db.get("table_count_query"),
        "data_versions": current_db.get("data_version_query")
        and candidate_db.get("data_version_query"),
        "jobs_inventory": jobs.get("status") == "PASSED",
        "persistent_roots": persistent_ok,
        "backup_location_external": backup.get("external_to_deployment_roots") is True,
        "reverse_proxy_auth_boundary": auth_boundary.get("status") == "PASSED",
        "free_disk_capacity": capacity.get("status") == "PASSED",
    }
    missing = [name for name, passed in checks.items() if not passed]
    violations = []
    if missing:
        violations.append("required fresh inventory evidence is missing: {}".format(
            ", ".join(missing)
        ))
    if capacity.get("status") == "FAILED":
        violations.append("fresh free-disk capacity is below the release formula")

    release_root = (
        getattr(args, "candidate_repository_root", None)
        or candidate.get("path") or ROOT
    )
    result = {
        "status": "PASSED" if not missing else (
            "FAILED" if capacity.get("status") == "FAILED" else "INCOMPLETE"
        ),
        "evidence_class": "fresh_production_inventory",
        "synthetic": False,
        "started_at": started,
        "finished_at": utc_iso(),
        "host_identity": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "release_identity": _release_identity_for_root(release_root)
        or generate_release_identity(repo_root=ROOT),
        "roots": {"current": current, "candidate": candidate},
        "configs": configs,
        "repositories": repo_observations,
        "services": services,
        "processes": processes,
        "ports": ports,
        "database_runtime_identity": db_inventory,
        "database_snapshots": {
            "current": {"schema": current_db.get("schema"),
                         "table_counts": current_db.get("table_counts"),
                         "data_versions": current_db.get("data_versions"),
                         "jobs": current_db.get("jobs")},
            "candidate": {"schema": candidate_db.get("schema"),
                           "table_counts": candidate_db.get("table_counts"),
                           "data_versions": candidate_db.get("data_versions"),
                           "jobs": candidate_db.get("jobs")},
        },
        "persistent_roots": persistent_roots,
        "jobs": jobs,
        "backup": backup,
        "reverse_proxy": proxy,
        "auth_boundary": auth_boundary,
        "disk": disk,
        "capacity_formula": capacity,
        "completeness": {"checks": checks, "missing": missing},
        "command_or_action": "python scripts/diagnostics/production_inventory.py",
        "exit_code": 0 if not missing else 1,
        "violations": violations,
    }
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--current-root", required=True)
    parser.add_argument("--candidate-root", required=True)
    parser.add_argument("--disk-root")
    parser.add_argument("--current-config")
    parser.add_argument("--candidate-config", "--config", dest="candidate_config")
    parser.add_argument("--current-repository-root")
    parser.add_argument("--candidate-repository-root")
    parser.add_argument("--repository-root", action="append")
    parser.add_argument("--service", action="append")
    parser.add_argument("--process-pattern", action="append")
    parser.add_argument("--port", action="append", type=int)
    parser.add_argument("--persistent-root", action="append")
    parser.add_argument("--jobs-root", action="append")
    parser.add_argument("--backup-root")
    parser.add_argument("--proxy-config", "--nginx-config", dest="proxy_config",
                        action="append")
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
