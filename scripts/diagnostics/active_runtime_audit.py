"""Audit evidence from an actually running VNext process.

The configured-runtime audit is deliberately separate. This command only
reports ``PASSED`` when it can correlate process/service identity, bound
configuration, release identity, database identity, and HTTP route evidence.
With no live target it returns ``PARTIAL`` rather than mislabelling a static
configuration check as an active-runtime check.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.config.runtime_config import load_application_config
from scripts.diagnostics.configured_runtime_audit import audit as audit_configured
from scripts.diagnostics.contract import with_contract


def _read_proc(pid):
    proc_root = "/proc/{}".format(int(pid))
    result = {"pid": int(pid), "available": False, "cmdline": [], "environment": {}}
    try:
        with open(os.path.join(proc_root, "cmdline"), "rb") as stream:
            result["cmdline"] = [
                item.decode("utf-8", "replace")
                for item in stream.read().split(b"\0") if item
            ]
        with open(os.path.join(proc_root, "environ"), "rb") as stream:
            for item in stream.read().split(b"\0"):
                if b"=" in item:
                    key, value = item.split(b"=", 1)
                    result["environment"][key.decode("utf-8", "replace")] = value.decode(
                        "utf-8", "replace"
                    )
        result["available"] = True
    except (OSError, ValueError):
        pass
    return result


def _read_pid_file(path):
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = int(stream.read().strip())
            return value if value > 0 else None
    except (OSError, TypeError, ValueError):
        return None


def _service_state(service):
    if not service:
        return {"requested": False}
    try:
        output = subprocess.check_output(
            ["systemctl", "show", str(service), "--property=ActiveState,MainPID,FragmentPath"],
            stderr=subprocess.STDOUT,
        ).decode("utf-8", "replace")
        values = dict(
            line.split("=", 1) for line in output.splitlines() if "=" in line
        )
        return {
            "requested": True, "available": True, "service": service,
            "properties": values,
        }
    except (OSError, subprocess.CalledProcessError) as exc:
        return {
            "requested": True, "available": False, "service": service,
            "error": str(exc),
        }


def _bound_config(process, explicit_path=None):
    """Extract the config selected by the live process, if observable."""
    cmdline = list(process.get("cmdline") or [])
    environment = process.get("environment") or {}
    selected = environment.get("COVERAGE_CONFIG_PATH") or ""
    for index, item in enumerate(cmdline):
        if item == "--config" and index + 1 < len(cmdline):
            selected = cmdline[index + 1]
            break
        if item.startswith("--config="):
            selected = item.split("=", 1)[1]
            break
    expected = os.path.realpath(explicit_path) if explicit_path else ""
    observed = os.path.realpath(selected) if selected else ""
    return {
        "selected_path": selected,
        "selected_realpath": observed,
        "expected_realpath": expected,
        "matches_requested": bool(observed and (not expected or observed == expected)),
        "source": "process_environment" if environment.get("COVERAGE_CONFIG_PATH") else (
            "process_cmdline" if selected else "unobserved"
        ),
    }


def _http_json(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read().decode("utf-8", "replace")
            return {"status": int(response.getcode()), "payload": json.loads(body)}
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return {"status": None, "error": str(exc)}


def _database_identity(config):
    db = dict(config.get("mysql") or {})
    result = {
        "configured_database": db.get("database", ""),
        "observed_database": None,
        "status": "NOT_PROBED",
    }
    try:
        import pymysql
        connection = pymysql.connect(
            host=db.get("host", "127.0.0.1"), port=int(db.get("port", 3306)),
            user=db.get("user", "root"), password=str(db.get("password", "")),
            database=db.get("database"), connect_timeout=3,
            cursorclass=pymysql.cursors.DictCursor,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT DATABASE() AS database_name")
                row = cursor.fetchone() or {}
                result["observed_database"] = row.get("database_name")
                result["status"] = (
                    "PASSED" if result["observed_database"] == result["configured_database"]
                    else "FAILED"
                )
        finally:
            connection.close()
    except Exception as exc:
        result["status"] = "UNAVAILABLE"
        result["error"] = str(exc)
    return result


def audit(repo_root=ROOT, url=None, pid=None, pid_file=None, service=None, config_path=None,
          require_live=False, probe_database=False):
    configured = audit_configured(repo_root)
    config = load_application_config(
        config_path, base_dir=repo_root
    ) if config_path else load_application_config(None, base_dir=repo_root)
    service_state = _service_state(service)
    resolved_pid = pid or _read_pid_file(pid_file)
    process = _read_proc(resolved_pid) if resolved_pid else {
        "available": False, "requested": bool(pid or pid_file),
    }
    if not pid and service_state.get("available"):
        main_pid = service_state.get("properties", {}).get("MainPID")
        if main_pid and main_pid != "0":
            process = _read_proc(int(main_pid))
            process["source"] = "systemd"
    bound_config = _bound_config(process, config_path)

    http = {}
    if url:
        base = str(url).rstrip("/")
        auth = config.get("auth") or {}
        operator_headers = {
            str(auth.get("user_header") or "X-Remote-User"): "runtime-audit"
        }
        http = {
            "health": _http_json(base + "/api/coverage/health"),
            "release": _http_json(base + "/api/coverage/release"),
            "routes": _http_json(base + "/api/coverage/routes", operator_headers),
        }

    release = (http.get("release") or {}).get("payload", {}).get("release") or {}
    route_payload = (http.get("routes") or {}).get("payload") or {}
    database = _database_identity(config) if probe_database else {
        "status": "NOT_PROBED",
        "configured_database": (config.get("mysql") or {}).get("database", ""),
    }
    checks = {
        "process_or_service": bool(
            process.get("available") or service_state.get("available")
        ),
        "bound_configuration": bool(
            process.get("available") and bound_config.get("matches_requested")
        ),
        "release_identity": bool(release.get("commit_sha") and release.get("build_id")),
        "database_identity": database.get("status") == "PASSED",
        "http_routes": bool(
            (http.get("health") or {}).get("status") == 200
            and (http.get("release") or {}).get("status") == 200
            and (http.get("routes") or {}).get("status") == 200
            and isinstance(route_payload.get("routes"), list)
        ),
    }
    missing = [name for name, passed in checks.items() if not passed]
    if not missing:
        status = "PASSED"
    elif require_live:
        status = "FAILED"
    else:
        status = "PARTIAL"
    return with_contract({
        "status": status,
        "evidence_class": "active_runtime_audit",
        "configured_runtime": configured,
        "process": process,
        "service": service_state,
        "pid_file": pid_file or "",
        "bound_configuration": bound_config,
        "http": http,
        "release": release,
        "database": database,
        "checks": checks,
        "missing_evidence": missing,
        "url": url or "",
    })


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--url")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--pid-file")
    parser.add_argument("--service")
    parser.add_argument("--config")
    parser.add_argument("--probe-database", action="store_true")
    parser.add_argument("--require-live", action="store_true")
    args = parser.parse_args(argv)
    result = audit(
        url=args.url, pid=args.pid, pid_file=args.pid_file, service=args.service,
        config_path=args.config, require_live=args.require_live,
        probe_database=args.probe_database,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASSED" or (
        result["status"] == "PARTIAL" and not args.require_live
    ) else 1


if __name__ == "__main__":
    sys.exit(main())
