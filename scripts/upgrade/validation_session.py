"""Owned-process validation sessions with deterministic teardown evidence."""

from __future__ import print_function

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.time_utils import utc_iso


LOOPBACK_HOSTS = frozenset(("127.0.0.1", "localhost", "::1", "[::1]"))
SESSION_SCHEMA_VERSION = 1


def _atomic_write(path, payload):
    path = os.path.abspath(path)
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    temporary = "{}.tmp-{}-{}".format(
        path, os.getpid(), int(time.time() * 1000000)
    )
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        try:
            os.fsync(stream.fileno())
        except OSError:
            pass
    os.replace(temporary, path)


def _load(path):
    with open(path, "r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("validation session manifest must be an object")
    return value


def _proc_state(pid):
    """Return the Linux process state, or ``None`` when unavailable."""
    path = "/proc/{}/stat".format(int(pid))
    try:
        with open(path, "r", encoding="utf-8") as stream:
            contents = stream.read()
        # The command name is enclosed in parentheses and may contain spaces;
        # use the final closing parenthesis before parsing the state field.
        closing = contents.rfind(")")
        fields = contents[closing + 2:].split() if closing >= 0 else []
        return fields[0] if fields else None
    except (OSError, IndexError, TypeError, ValueError):
        return None


def _pid_exists(pid):
    try:
        os.kill(int(pid), 0)
        # ``kill(pid, 0)`` also succeeds for a zombie.  It has already
        # released its resources and cannot keep a validation port alive, so
        # it must not make teardown fail forever when another owner must reap
        # the child process.
        return _proc_state(pid) != "Z"
    except (OSError, ValueError):
        return False


def _proc_start_time(pid):
    """Return Linux proc start ticks when available for PID reuse safety."""
    path = "/proc/{}/stat".format(int(pid))
    try:
        with open(path, "r", encoding="utf-8") as stream:
            contents = stream.read()
        # ``comm`` (field 2) is enclosed in parentheses and may contain
        # spaces.  Splitting the whole line shifts the fixed field indexes;
        # parse the fields after the final closing parenthesis instead.
        closing = contents.rfind(")")
        fields = contents[closing + 2:].split() if closing >= 0 else []
        # The remainder starts at field 3 (state), so field 22
        # (starttime) is offset 19 within it.
        return int(fields[19])
    except (OSError, IndexError, TypeError, ValueError):
        return None


def _parse_listener_output(output):
    listeners = []
    for line in output.splitlines():
        fields = line.split()
        if not fields or fields[0].lower() in ("state", "active"):
            continue
        # ``ss -lnt`` puts Local Address:Port at column four; netstat uses
        # the same position but prefixes it with the protocol.
        if len(fields) < 4:
            continue
        endpoint = fields[3].rsplit(":", 1)
        if len(endpoint) != 2:
            continue
        try:
            port = int(endpoint[1].strip("[]"))
        except ValueError:
            continue
        listeners.append({"address": endpoint[0], "port": port})
    return listeners


def _port_listeners():
    """Read listening TCP ports without requiring systemd or root access.

    ``None`` means the probe could not run.  Treating an unavailable probe as
    an empty listener set would make teardown falsely pass, which is unsafe
    for a release-window validation service.
    """
    for command in (("ss", "-lnt"), ("netstat", "-lnt")):
        try:
            output = subprocess.check_output(
                list(command), stderr=subprocess.STDOUT,
                universal_newlines=True,
            )
        except (OSError, subprocess.CalledProcessError):
            continue
        return _parse_listener_output(output)
    return None


def validate_bind(host, port, allow_non_loopback=False, allowlist=None,
                  temporary_token="", expires_at=""):
    """Enforce loopback-by-default validation service binding."""
    host = str(host or "127.0.0.1").strip().lower()
    port = int(port)
    if host in LOOPBACK_HOSTS:
        return {"status": "PASSED", "host": host, "port": port,
                "exposure": "loopback"}
    if not allow_non_loopback:
        raise ValueError("validation services must bind to loopback by default")
    if not allowlist:
        raise ValueError("non-loopback validation bind requires an allowlist")
    if not temporary_token:
        raise ValueError("non-loopback validation bind requires a temporary token")
    if not expires_at:
        raise ValueError("non-loopback validation bind requires an expiry")
    return {
        "status": "PASSED", "host": host, "port": port,
        "exposure": "explicit_non_loopback",
        "allowlist": sorted(set(str(item) for item in allowlist)),
        "temporary_token": str(temporary_token),
        "expires_at": str(expires_at),
    }


class ValidationSession(object):
    """Own PIDs and ports for one Candidate/Baseline validation session."""

    def __init__(self, manifest_path, data=None):
        self.manifest_path = os.path.abspath(manifest_path)
        self.data = dict(data or {})

    @classmethod
    def create(cls, manifest_path, session_id, candidate_sha="", baseline_sha="",
               pids=None, ports=None, listeners=None, evidence_paths=None,
               expires_at="", binds=None):
        session_id = str(session_id or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        if binds:
            for item in binds:
                validate_bind(
                    item.get("host"), item.get("port"),
                    allow_non_loopback=bool(item.get("allow_non_loopback")),
                    allowlist=item.get("allowlist"),
                    temporary_token=item.get("temporary_token", ""),
                    expires_at=item.get("expires_at", ""),
                )
        value = cls(manifest_path, {
            "schema_version": SESSION_SCHEMA_VERSION,
            "session_id": session_id,
            "candidate_sha": str(candidate_sha or ""),
            "baseline_sha": str(baseline_sha or ""),
            "pids": sorted(set(int(pid) for pid in (pids or []))),
            "ports": sorted(set(int(port) for port in (ports or []))),
            "listeners": list(listeners or []),
            "binds": [dict(item) for item in (binds or [])],
            "created_at": utc_iso(),
            "expires_at": str(expires_at or ""),
            "evidence_paths": sorted(set(str(path) for path in (evidence_paths or []))),
            "teardown_status": "NOT_STARTED",
            "teardown_evidence": {},
        })
        value._record_pid_start_times()
        value.save()
        return value

    @classmethod
    def load(cls, manifest_path):
        return cls(manifest_path, _load(manifest_path))

    def _record_pid_start_times(self):
        start_times = {}
        for pid in self.data.get("pids") or []:
            if _pid_exists(pid):
                # A present key with a null value means the process was
                # observed and explicitly owned, but its platform start time
                # could not be read.  An absent key means it was not owned at
                # session creation and must never become signalable later.
                start_times[str(pid)] = _proc_start_time(pid)
        self.data["pid_start_times"] = start_times

    def save(self):
        _atomic_write(self.manifest_path, self.data)

    def add_process(self, pid, port=None, listener=None):
        pid = int(pid)
        self.data.setdefault("pids", [])
        if pid not in self.data["pids"]:
            self.data["pids"].append(pid)
            self.data["pids"].sort()
        start_times = self.data.setdefault("pid_start_times", {})
        if _pid_exists(pid):
            start_times[str(pid)] = _proc_start_time(pid)
        else:
            # Keep an unowned PID in the manifest for teardown evidence, but
            # do not create an ownership record that could authorize a future
            # PID-reused process.
            start_times.pop(str(pid), None)
        if port is not None:
            self.data.setdefault("ports", [])
            if int(port) not in self.data["ports"]:
                self.data["ports"].append(int(port))
                self.data["ports"].sort()
        if listener is not None:
            self.data.setdefault("listeners", []).append(dict(listener))
        self.save()

    def _owned_pid(self, pid):
        start_times = self.data.get("pid_start_times") or {}
        if str(pid) not in start_times:
            return False
        expected = start_times.get(str(pid))
        actual = _proc_start_time(pid)
        # If /proc is unavailable, retain the explicit manifest ownership. On
        # Linux, a changed start time means the PID was recycled and is not
        # safe to signal.
        return expected is None or actual is None or int(expected) == int(actual)

    def stop_owned_processes(self, timeout=10.0):
        attempted = []
        for pid in list(self.data.get("pids") or []):
            if not _pid_exists(pid):
                continue
            if not self._owned_pid(pid):
                attempted.append({"pid": int(pid), "status": "PID_REUSED"})
                continue
            attempted.append({"pid": int(pid), "status": "SIGTERM"})
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass
        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            if not any(_pid_exists(pid) and self._owned_pid(pid)
                       for pid in self.data.get("pids") or []):
                break
            time.sleep(0.1)
        for pid in list(self.data.get("pids") or []):
            if _pid_exists(pid) and self._owned_pid(pid):
                attempted.append({"pid": int(pid), "status": "SIGKILL"})
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except OSError:
                    pass
        return attempted

    def verify_teardown(self):
        remaining_pids = [int(pid) for pid in self.data.get("pids") or []
                          if _pid_exists(pid) and self._owned_pid(pid)]
        reused_pids = [int(pid) for pid in self.data.get("pids") or []
                       if _pid_exists(pid) and not self._owned_pid(pid)]
        listeners = _port_listeners()
        expected_ports = set(int(port) for port in self.data.get("ports") or [])
        remaining_ports = ([item for item in listeners
                            if int(item.get("port") or 0) in expected_ports]
                           if listeners is not None else [])
        ports_probe_ok = listeners is not None or not expected_ports
        return {
            "status": "PASSED" if (
                not remaining_pids and not reused_pids and
                not remaining_ports and ports_probe_ok
            ) else "FAILED",
            "pids_closed": not remaining_pids and not reused_pids,
            "ports_closed": not remaining_ports and ports_probe_ok,
            "ports_probe_ok": ports_probe_ok,
            "remaining_pids": remaining_pids,
            "reused_pids": reused_pids,
            "remaining_ports": remaining_ports,
        }

    def teardown(self, evidence_path="", timeout=10.0):
        attempted = self.stop_owned_processes(timeout=timeout)
        verification = self.verify_teardown()
        status = verification["status"]
        evidence = {
            "schema_version": SESSION_SCHEMA_VERSION,
            "session_id": self.data.get("session_id", ""),
            "teardown_at": utc_iso(),
            "status": status,
            # Keep the hard-gate fields at the top level as well as in the
            # detailed verification object.  Release evidence consumers must
            # be able to make a direct, unambiguous decision without knowing
            # the internal ValidationSession result shape.
            "pids_closed": verification["pids_closed"],
            "ports_closed": verification["ports_closed"],
            "ports_probe_ok": verification["ports_probe_ok"],
            "attempted": attempted,
            "verification": verification,
            "p1": status != "PASSED",
        }
        self.data["teardown_status"] = status
        self.data["teardown_evidence"] = evidence
        self.save()
        target = evidence_path or ""
        if target:
            _atomic_write(target, evidence)
        return evidence


def main(argv=None):
    parser = argparse.ArgumentParser(prog="validation_session.py")
    parser.add_argument("action", choices=("create", "add-process", "teardown", "verify"))
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--session-id", default="")
    parser.add_argument("--candidate-sha", default="")
    parser.add_argument("--baseline-sha", default="")
    parser.add_argument("--pid", action="append", type=int, default=[])
    parser.add_argument("--port", action="append", type=int, default=[])
    parser.add_argument("--evidence", default="")
    args = parser.parse_args(argv)
    if args.action == "create":
        session = ValidationSession.create(
            args.manifest, args.session_id,
            candidate_sha=args.candidate_sha, baseline_sha=args.baseline_sha,
            pids=args.pid, ports=args.port,
        )
        result = session.data
    else:
        session = ValidationSession.load(args.manifest)
        if args.action == "add-process":
            if len(args.pid) != 1:
                parser.error("add-process requires exactly one --pid")
            session.add_process(args.pid[0], args.port[0] if args.port else None)
            result = session.data
        elif args.action == "teardown":
            result = session.teardown(args.evidence)
        else:
            result = session.verify_teardown()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("status", "PASSED") == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
