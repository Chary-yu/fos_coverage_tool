"""Ownership-aware cleanup gate for stale validation sessions.

Ports are discovery hints, never kill authority.  A process is safe to tear
down only when PID, command line, cwd, candidate root, port and session
identity all bind to the same configured validation attempt.
"""

from __future__ import print_function

import os
import re
import signal
import subprocess


PASSED = "PASSED"
SAFE_TO_TEARDOWN = "SAFE_TO_TEARDOWN"
UNKNOWN_PROCESS = "UNKNOWN_PROCESS"
BLOCKED = "BLOCKED"


def _real(path):
    return os.path.realpath(os.path.abspath(str(path)))


def _inside(root, path):
    try:
        return os.path.commonpath((_real(root), _real(path))) == _real(root)
    except (AttributeError, OSError, ValueError):
        return False


def _sequence(value):
    if value in (None, ""):
        return []
    if isinstance(value, (str, bytes, int)):
        return [value]
    return value


def _configured_residue_paths(roots):
    paths = []
    for configured in _sequence(roots):
        root = _real(configured)
        if os.path.basename(root).startswith(("gate_", "release_validation_")):
            if os.path.lexists(root):
                paths.append(root)
            continue
        if not os.path.isdir(root):
            continue
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            if name.startswith(("gate_", "release_validation_")):
                path = os.path.join(root, name)
                if os.path.lexists(path):
                    paths.append(_real(path))
    return sorted(set(paths))


def _process_inventory():
    """Return conservative process records from procfs/ps."""
    try:
        output = subprocess.check_output(
            ["ps", "-eo", "pid=,args="], stderr=subprocess.STDOUT
        ).decode("utf-8", "replace")
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        return []
    records = []
    for line in output.splitlines():
        match = re.match(r"\s*(\d+)\s+(.*)$", line)
        if not match:
            continue
        pid = int(match.group(1))
        cmdline = match.group(2).strip()
        proc_root = "/proc/{}".format(pid)
        try:
            cwd = os.path.realpath(os.path.join(proc_root, "cwd"))
        except OSError:
            cwd = ""
        try:
            raw_cmdline = open(
                os.path.join(proc_root, "cmdline"), "rb"
            ).read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
            if raw_cmdline:
                cmdline = raw_cmdline
        except OSError:
            pass
        records.append({"pid": pid, "cmdline": cmdline, "cwd": cwd})
    return records


def _listener_inventory():
    try:
        output = subprocess.check_output(
            ["ss", "-lntp"], stderr=subprocess.STDOUT
        ).decode("utf-8", "replace")
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        return []
    result = []
    for line in output.splitlines():
        ports = re.findall(r":(\d+)\s", line + " ")
        if not ports:
            continue
        pid_matches = re.findall(r"pid=(\d+)", line)
        for port in ports:
            result.append({
                "port": int(port),
                "pid": int(pid_matches[0]) if pid_matches else None,
                "raw": line,
            })
    return result


def _normalize_process(record):
    result = dict(record or {})
    try:
        result["pid"] = int(result.get("pid"))
    except (TypeError, ValueError):
        result["pid"] = None
    try:
        result["port"] = int(result.get("port"))
    except (TypeError, ValueError):
        result["port"] = None
    result["cmdline"] = str(result.get("cmdline") or "")
    result["cwd"] = _real(result["cwd"]) if result.get("cwd") else ""
    result["candidate_root"] = (
        _real(result["candidate_root"]) if result.get("candidate_root") else ""
    )
    result["session_identity"] = str(result.get("session_identity") or "")
    return result


def _binding_errors(process, candidate_root, port, session_identity):
    errors = []
    if not process.get("pid"):
        errors.append("pid")
    if not process.get("cmdline"):
        errors.append("cmdline")
    elif _real(candidate_root) not in process["cmdline"]:
        errors.append("cmdline.candidate_root")
    if not process.get("cwd") or not _inside(candidate_root, process["cwd"]):
        errors.append("cwd")
    if process.get("candidate_root") != _real(candidate_root):
        errors.append("candidate_root")
    if process.get("port") != int(port):
        errors.append("port")
    if process.get("session_identity") != str(session_identity):
        errors.append("session_identity")
    if str(session_identity) not in process.get("cmdline", ""):
        errors.append("cmdline.session_identity")
    return errors


def scan_validation_residue(candidate_roots=(), ports=(), session_identity="",
                            processes=None, listeners=None):
    """Scan configured residue and return a fail-closed ownership decision."""
    residue_paths = _configured_residue_paths(candidate_roots)
    configured_ports = []
    for port in _sequence(ports):
        try:
            configured_ports.append(int(port))
        except (TypeError, ValueError):
            raise ValueError("validation residue ports must be integers")
    configured_ports = sorted(set(configured_ports))
    process_records = [
        _normalize_process(record) for record in (
            _process_inventory() if processes is None else processes
        )
    ]
    listener_records = list(
        _listener_inventory() if listeners is None else (listeners or [])
    )
    observations = []
    blocked = []
    safe = []

    for path in residue_paths:
        matching = [
            record for record in process_records
            if record.get("candidate_root") == path or _inside(path, record.get("cwd", ""))
        ]
        if not matching:
            blocked.append({
                "status": UNKNOWN_PROCESS, "candidate_root": path,
                "reason": "residue_has_no_owned_process_record",
            })
            continue
        for process in matching:
            candidate_port = process.get("port")
            if candidate_port not in configured_ports:
                blocked.append({
                    "status": UNKNOWN_PROCESS, "candidate_root": path,
                    "pid": process.get("pid"), "reason": "port_not_bound_to_session",
                    "binding_errors": ["port"],
                })
                continue
            errors = _binding_errors(
                process, path, candidate_port, session_identity
            )
            observation = dict(process)
            observation.update({
                "candidate_root": path,
                "expected_port": candidate_port,
                "expected_session_identity": str(session_identity),
                "binding_errors": errors,
                "status": SAFE_TO_TEARDOWN if not errors else UNKNOWN_PROCESS,
            })
            observations.append(observation)
            (safe if not errors else blocked).append(observation)

    for listener in listener_records:
        try:
            port = int(listener.get("port"))
        except (TypeError, ValueError):
            continue
        if port not in configured_ports:
            continue
        pid = listener.get("pid")
        if not any(item.get("pid") == pid and item in safe for item in observations):
            blocked.append({
                "status": UNKNOWN_PROCESS, "port": port, "pid": pid,
                "reason": "listener_not_fully_bound_to_candidate_session",
            })

    configured_listener_ports = set(
        int(item.get("port")) for item in listener_records
        if str(item.get("port") or "").isdigit() and
        int(item.get("port")) in configured_ports
    )
    if blocked:
        status = BLOCKED
    elif safe:
        status = SAFE_TO_TEARDOWN
    elif residue_paths or configured_listener_ports:
        # A configured session/port without a complete identity is not an
        # empty scan; it is an unknown process and must block teardown.
        status = BLOCKED
        blocked.append({
            "status": UNKNOWN_PROCESS,
            "reason": "configured_validation_identity_not_proven",
        })
    else:
        status = PASSED
    return {
        "status": status,
        "candidate_roots": residue_paths,
        "ports": configured_ports,
        "session_identity": str(session_identity or ""),
        "observations": observations,
        "safe_to_teardown": safe,
        "blocked": blocked,
        "teardown_authorized": status == SAFE_TO_TEARDOWN,
    }


def teardown_validation_residue(candidate_roots=(), ports=(), session_identity="",
                                processes=None, listeners=None, killer=None):
    """Terminate only records that passed the complete ownership gate."""
    report = scan_validation_residue(
        candidate_roots, ports, session_identity,
        processes=processes, listeners=listeners,
    )
    if report.get("status") != SAFE_TO_TEARDOWN:
        report["teardown_status"] = BLOCKED
        return report
    killer = killer or os.kill
    killed = []
    for process in report.get("safe_to_teardown") or []:
        pid = int(process["pid"])
        killer(pid, signal.SIGTERM)
        killed.append(pid)
    report["killed_pids"] = killed
    report["teardown_status"] = PASSED
    return report
