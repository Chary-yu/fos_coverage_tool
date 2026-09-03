"""Ownership-aware cleanup gate for stale validation sessions.

Ports are discovery hints, never kill authority.  A process is safe to tear
down only when PID, command line, cwd, candidate root, port and session
identity all bind to the same configured validation attempt.
"""

from __future__ import print_function

import os
import json
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


def _manifest_paths(candidate_roots, session_manifests=None):
    """Return explicit and discoverable validation-session manifests.

    A process inventory intentionally contains only observations available
    from procfs/ps.  Ownership is supplied separately by a session manifest;
    keeping the two inputs separate prevents tests from accidentally proving
    a record that the real inventory cannot produce.
    """
    paths = []
    for configured in _sequence(session_manifests):
        if configured:
            path = _real(configured)
            if os.path.isfile(path) and not os.path.islink(path):
                paths.append(path)
    for root in _configured_residue_paths(candidate_roots):
        if not os.path.isdir(root):
            continue
        for directory, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs if not os.path.islink(
                os.path.join(directory, name)
            )]
            for name in files:
                lowered = name.lower()
                if not lowered.endswith(".json") or "session" not in lowered:
                    continue
                path = os.path.join(directory, name)
                if not os.path.islink(path):
                    paths.append(_real(path))
    return sorted(set(paths))


def _manifest_process_inventory(candidate_roots, session_manifests=None):
    """Extract PID/root/session/port ownership from JSON manifests.

    The parser accepts both :class:`ValidationSession` manifests and the
    older Gate-E-shaped records.  Malformed or incomplete manifests simply
    contribute no ownership evidence; the caller will then fail closed.
    """
    residue_paths = _configured_residue_paths(candidate_roots)
    records = []
    for manifest_path in _manifest_paths(candidate_roots, session_manifests):
        try:
            with open(manifest_path, "r", encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        session = str(
            payload.get("session_id") or
            payload.get("validation_session_id") or
            payload.get("release_validation_session_id") or
            payload.get("session_identity") or ""
        ).strip()
        root_value = (
            payload.get("candidate_root") or
            payload.get("candidate_root_path") or
            payload.get("validation_candidate_root") or
            payload.get("root") or ""
        )
        root = _real(root_value) if root_value else ""
        if not root:
            manifest_directory = os.path.dirname(_real(manifest_path))
            for residue_path in residue_paths:
                if _inside(residue_path, manifest_directory):
                    root = residue_path
                    break

        global_ports = []
        for value in _sequence(payload.get("ports")):
            try:
                global_ports.append(int(value))
            except (TypeError, ValueError):
                pass
        process_values = []
        raw_processes = payload.get("processes")
        if isinstance(raw_processes, dict):
            raw_processes = list(raw_processes.values())
        for item in _sequence(raw_processes):
            if isinstance(item, dict):
                process_values.append(item)
            else:
                process_values.append({"pid": item})
        for value in _sequence(payload.get("pids")):
            if isinstance(value, dict):
                process_values.append(value)
            else:
                process_values.append({"pid": value})
        for item in _sequence(payload.get("listeners")):
            if isinstance(item, dict) and item.get("pid") is not None:
                process_values.append(item)

        if not process_values:
            # A manifest without a PID cannot authorize a process, but retain
            # no synthetic record: a listener PID must be joined to a
            # manifest PID below rather than inferred from a session string.
            continue
        for item in process_values:
            try:
                pid = int(item.get("pid"))
            except (AttributeError, TypeError, ValueError):
                continue
            ports = list(global_ports)
            for key in ("port", "listen_port"):
                if item.get(key) not in (None, ""):
                    try:
                        ports.append(int(item.get(key)))
                    except (TypeError, ValueError):
                        pass
            for listener in _sequence(item.get("listeners")):
                if isinstance(listener, dict) and listener.get("port") is not None:
                    try:
                        ports.append(int(listener.get("port")))
                    except (TypeError, ValueError):
                        pass
            records.append({
                "pid": pid,
                "candidate_root": root,
                "session_identity": session,
                "ports": sorted(set(ports)),
                "manifest_path": manifest_path,
            })
    return records


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


def _join_process_listeners_and_manifests(processes, listeners, manifests):
    """Join raw process observations to listener and session ownership data."""
    listener_ports = {}
    for listener in listeners or []:
        try:
            pid = int(listener.get("pid"))
            port = int(listener.get("port"))
        except (AttributeError, TypeError, ValueError):
            continue
        listener_ports.setdefault(pid, set()).add(port)

    ownership = {}
    for manifest in manifests or []:
        try:
            pid = int(manifest.get("pid"))
        except (AttributeError, TypeError, ValueError):
            continue
        ownership.setdefault(pid, []).append(manifest)

    joined = []
    for original in processes or []:
        normalized = _normalize_process(original)
        pid = normalized.get("pid")
        observed_ports = sorted(listener_ports.get(pid, set()))
        owned = ownership.get(pid) or []
        if not observed_ports:
            # Do not trust a port supplied on a process record.  Real
            # _process_inventory() never supplies one; the independent
            # listener inventory is the only port authority.
            observed_ports = []
        if owned:
            for manifest in owned:
                manifest_ports = set(manifest.get("ports") or [])
                ports = [port for port in observed_ports
                         if not manifest_ports or port in manifest_ports]
                if not ports:
                    ports = [None]
                for port in ports:
                    item = dict(normalized)
                    item.update({
                        "candidate_root": manifest.get("candidate_root", ""),
                        "session_identity": manifest.get("session_identity", ""),
                        "port": port,
                        "ownership_manifest": manifest.get("manifest_path", ""),
                    })
                    joined.append(item)
            continue

        # Keep backwards-compatible support for already-enriched callers,
        # while real procfs records still remain unowned and fail closed.
        for port in observed_ports or [None]:
            item = dict(normalized)
            if not item.get("candidate_root") and not item.get("session_identity"):
                item["candidate_root"] = ""
                item["session_identity"] = ""
            item["port"] = port
            joined.append(item)
    return joined


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
                            processes=None, listeners=None,
                            session_manifests=None):
    """Scan configured residue and return a fail-closed ownership decision."""
    residue_paths = _configured_residue_paths(candidate_roots)
    configured_ports = []
    for port in _sequence(ports):
        try:
            configured_ports.append(int(port))
        except (TypeError, ValueError):
            raise ValueError("validation residue ports must be integers")
    configured_ports = sorted(set(configured_ports))
    raw_processes = _process_inventory() if processes is None else (processes or [])
    listener_records = list(
        _listener_inventory() if listeners is None else (listeners or [])
    )
    process_records = _join_process_listeners_and_manifests(
        raw_processes, listener_records,
        _manifest_process_inventory(candidate_roots, session_manifests),
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
                                processes=None, listeners=None, killer=None,
                                session_manifests=None):
    """Terminate only records that passed the complete ownership gate."""
    report = scan_validation_residue(
        candidate_roots, ports, session_identity,
        processes=processes, listeners=listeners,
        session_manifests=session_manifests,
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
