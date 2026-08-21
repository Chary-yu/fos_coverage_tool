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
try:
    from urllib.parse import urlparse
except ImportError:  # pragma: no cover - Python 2 compatibility is not required at runtime
    from urlparse import urlparse

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.config.runtime_config import load_application_config
from scripts.diagnostics.configured_runtime_audit import audit as audit_configured
from scripts.diagnostics.contract import with_contract


def _git_revision(repo_root):
    """Return the exact checkout revision used as the runtime expectation."""
    try:
        return subprocess.check_output(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            stderr=subprocess.STDOUT,
        ).decode("ascii").strip()
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        return ""


def _release_matches_revision(release, expected_revision):
    """Require the live release endpoint to identify the expected SHA."""
    release = release if isinstance(release, dict) else {}
    actual = str(release.get("commit_sha") or "").strip()
    expected = str(expected_revision or "").strip()
    return bool(actual and expected and actual == expected)


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
    source = "process_environment" if environment.get("COVERAGE_CONFIG_PATH") else (
        "process_cmdline" if selected else "implicit_default"
    )
    # A process started without --config is still bound to the repository's
    # default configuration. Record that fact explicitly; otherwise an audit
    # with no explicit --config would treat a live process as unbound merely
    # because the default is implicit in the command line.
    if not selected and process.get("available"):
        repo_root = process.get("repo_root") or ""
        default_path = os.path.join(repo_root, "coverage_config.json") if repo_root else ""
        if default_path and os.path.isfile(default_path):
            selected = default_path
    repo_root = process.get("repo_root") or ""
    expected_value = explicit_path
    if expected_value and not os.path.isabs(str(expected_value)) and repo_root:
        expected_value = os.path.join(repo_root, str(expected_value))
    observed_value = selected
    if observed_value and not os.path.isabs(str(observed_value)) and repo_root:
        observed_value = os.path.join(repo_root, str(observed_value))
    expected = os.path.realpath(expected_value) if expected_value else ""
    observed = os.path.realpath(observed_value) if observed_value else ""
    return {
        "selected_path": selected,
        "selected_realpath": observed,
        "expected_realpath": expected,
        "matches_requested": bool(observed and (not expected or observed == expected)),
        "source": source if observed else "unobserved",
    }


def _listening_ports(pid):
    """Return TCP listening ports visible to a process on Linux.

    HTTP health checks prove that a route exists, but do not prove that the
    process selected the configured bind port. The procfs check adds that
    identity evidence without opening sockets or changing runtime state. On a
    non-Linux host it returns an empty set and the active audit remains
    explicitly partial.
    """
    if not pid:
        return []
    ports = set()
    for template in ("/proc/{}/net/tcp", "/proc/{}/net/tcp6"):
        filename = template.format(int(pid))
        try:
            with open(filename, "r", encoding="ascii") as stream:
                for line in stream.read().splitlines()[1:]:
                    fields = line.split()
                    if len(fields) < 4 or fields[3] != "0A":
                        continue
                    address = fields[1]
                    port = address.rsplit(":", 1)[-1]
                    ports.add(int(port, 16))
        except (OSError, ValueError, IndexError):
            continue
    return sorted(ports)


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
        "observed_host": None,
        "observed_port": None,
        "schema_meta": [],
        "schema_ready": False,
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
                cursor.execute(
                    "SELECT DATABASE() AS database_name, @@hostname AS hostname, "
                    "@@port AS server_port"
                )
                row = cursor.fetchone() or {}
                result["observed_database"] = row.get("database_name")
                result["observed_host"] = row.get("hostname")
                result["observed_port"] = row.get("server_port")
                cursor.execute(
                    "SELECT schema_key, schema_version FROM coverage_schema_meta"
                )
                result["schema_meta"] = [dict(item) for item in (cursor.fetchall() or [])]
                result["schema_ready"] = any(
                    str(item.get("schema_key") or "") == "coverage_vnext_core" and
                    int(item.get("schema_version") or 0) >= 1
                    for item in result["schema_meta"]
                )
                result["status"] = (
                    "PASSED" if result["observed_database"] == result["configured_database"]
                    and result["schema_ready"] else "FAILED"
                )
        finally:
            connection.close()
    except Exception as exc:
        result["status"] = "UNAVAILABLE"
        result["error"] = str(exc)
    return result


def audit(repo_root=ROOT, url=None, pid=None, pid_file=None, service=None, config_path=None,
          require_live=False, probe_database=False, expected_revision=None):
    expected_revision = str(
        expected_revision or os.environ.get("COVERAGE_EXPECTED_REVISION") or
        _git_revision(repo_root)
    ).strip()
    configured = audit_configured(repo_root)
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
    process["repo_root"] = os.path.abspath(repo_root)
    bound_config = _bound_config(process, config_path)
    bound_config_path = bound_config.get("selected_path") or config_path or None
    config_error = ""
    try:
        # Probe the configuration actually selected by the live process. This
        # prevents an audit launched without --config from probing the checked
        # in default DB while the process is serving the Candidate DB.
        config = load_application_config(bound_config_path, base_dir=repo_root)
    except (OSError, ValueError, RuntimeError) as exc:
        config_error = str(exc)
        config = load_application_config(None, base_dir=repo_root)
    bound_config["config_load_error"] = config_error
    bound_config["runtime_mode"] = config.get("runtime_mode")
    bound_config["database"] = (config.get("mysql") or {}).get("database", "")
    bound_config["server"] = config.get("server") or {}

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
    release_matches_revision = _release_matches_revision(release, expected_revision)
    route_payload = (http.get("routes") or {}).get("payload") or {}
    database = _database_identity(config) if probe_database else {
        "status": "NOT_PROBED",
        "configured_database": (config.get("mysql") or {}).get("database", ""),
        "schema_ready": False,
    }
    listening_ports = _listening_ports(process.get("pid")) if process.get("available") else []
    configured_port = int((config.get("server") or {}).get("port") or 0)
    try:
        requested_url_port = urlparse(str(url)).port if url else None
    except ValueError:
        requested_url_port = None
    checks = {
        "process_or_service": bool(
            process.get("available") or service_state.get("available")
        ),
        "bound_configuration": bool(
            process.get("available") and bound_config.get("matches_requested") and
            not bound_config.get("config_load_error") and
            str(config.get("runtime_mode") or "").lower() == "vnext"
        ),
        "listening_port": bool(
            process.get("available") and configured_port and
            configured_port in listening_ports and
            (requested_url_port is None or requested_url_port == configured_port)
        ),
        "release_identity": bool(
            release.get("commit_sha") and release.get("build_id") and
            release_matches_revision
        ),
        "database_identity": database.get("status") == "PASSED" and
            bool(database.get("schema_ready")),
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
        "expected_revision": expected_revision,
        "release_matches_revision": release_matches_revision,
        "database": database,
        "listening_ports": listening_ports,
        "config_load_error": config_error,
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
    parser.add_argument(
        "--expected-revision",
        help="exact commit SHA that the live release endpoint must report; defaults to HEAD",
    )
    args = parser.parse_args(argv)
    result = audit(
        url=args.url, pid=args.pid, pid_file=args.pid_file, service=args.service,
        config_path=args.config, require_live=args.require_live,
        probe_database=args.probe_database, expected_revision=args.expected_revision,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASSED" or (
        result["status"] == "PARTIAL" and not args.require_live
    ) else 1


if __name__ == "__main__":
    sys.exit(main())
