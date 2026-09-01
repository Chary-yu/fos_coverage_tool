"""Small process controller used by the isolated local staging rehearsal.

It starts the real ``enhance_coverage.py server`` with the supplied staging
configuration.  Production deployments must provide their own process
supervisor commands through the same lifecycle interface.
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.upgrade.validation_session import (
    ValidationSession, _pid_exists, validate_bind,
)


def _read_pid(path):
    with open(path, "r", encoding="utf-8") as stream:
        return int(stream.read().strip())


def _atomic_write_json(path, payload):
    path = os.path.abspath(path)
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    temporary = "{}.tmp-{}-{}".format(path, os.getpid(), uuid.uuid4().hex[:12])
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        try:
            os.fsync(stream.fileno())
        except OSError:
            pass
    os.replace(temporary, path)


def _load_json(path):
    try:
        with open(os.path.abspath(path), "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _load_server_binding(config_path):
    with open(config_path, "r", encoding="utf-8") as stream:
        config = json.load(stream)
    server = config.get("server") or {}
    host = str(server.get("host") or "127.0.0.1")
    port = int(server.get("port") or 0)
    if port <= 0 or port > 65535:
        raise ValueError("staging server port is invalid")
    return host, port


def _get_or_create_session(manifest_path, session_id, candidate_sha,
                           baseline_sha, host, port, allow_non_loopback,
                           allowlist, temporary_token, expires_at,
                           reset_completed=False):
    bind = validate_bind(
        host, port, allow_non_loopback=allow_non_loopback,
        allowlist=allowlist, temporary_token=temporary_token,
        expires_at=expires_at,
    )
    bind.update({
        "allow_non_loopback": bool(allow_non_loopback),
        "temporary_token": str(temporary_token or ""),
        "expires_at": str(expires_at or ""),
    })
    if os.path.isfile(manifest_path):
        session = ValidationSession.load(manifest_path)
        if session_id and session.data.get("session_id") != session_id:
            raise ValueError("validation session id does not match manifest")
        if reset_completed and session.data.get("teardown_status") == "PASSED":
            return ValidationSession.create(
                manifest_path, session.data.get("session_id") or session_id,
                candidate_sha=candidate_sha, baseline_sha=baseline_sha,
                ports=[port], binds=[bind],
            )
        return session
    resolved_id = str(session_id or "staging-{}-{}".format(
        os.getpid(), uuid.uuid4().hex[:12]
    ))
    return ValidationSession.create(
        manifest_path, resolved_id, candidate_sha=candidate_sha,
        baseline_sha=baseline_sha, ports=[port], binds=[bind],
    )


def start(config_path, pid_path, endpoint, session_manifest="",
          session_id="", candidate_sha="", baseline_sha="",
          allow_non_loopback=False, allowlist=None, temporary_token="",
          expires_at="", serving=False, serving_state_path=""):
    host, port = _load_server_binding(config_path)
    session_prefix = "COVERAGE_SERVING" if serving else "COVERAGE_VALIDATION"
    fallback_prefix = "COVERAGE_VALIDATION" if serving else "COVERAGE_SERVING"
    session_manifest = session_manifest or os.environ.get(
        session_prefix + "_SESSION_MANIFEST", ""
    ) or os.environ.get(
        fallback_prefix + "_SESSION_MANIFEST", ""
    ) or (pid_path + ".session.json")
    session_id = session_id or os.environ.get(
        session_prefix + "_SESSION_ID", ""
    ) or os.environ.get(
        fallback_prefix + "_SESSION_ID", ""
    )
    candidate_sha = candidate_sha or os.environ.get(
        session_prefix + "_CANDIDATE_SHA", ""
    ) or os.environ.get(
        fallback_prefix + "_CANDIDATE_SHA", ""
    )
    baseline_sha = baseline_sha or os.environ.get(
        session_prefix + "_BASELINE_SHA", ""
    ) or os.environ.get(
        fallback_prefix + "_BASELINE_SHA", ""
    )
    session = _get_or_create_session(
        session_manifest, session_id, candidate_sha, baseline_sha,
        host, port, allow_non_loopback, allowlist or [], temporary_token,
        expires_at, reset_completed=serving,
    )

    if serving:
        serving_state_path = serving_state_path or os.environ.get(
            "COVERAGE_SERVING_STATE_PATH", ""
        ) or (pid_path + ".current.json")
        serving_state_path = os.path.abspath(serving_state_path)

    def record_serving_state(status, pid, **extra):
        if not serving:
            return
        payload = {
            "schema_version": 1,
            "role": "production_serving",
            "status": status,
            "session_id": session.data.get("session_id", ""),
            "session_manifest": os.path.abspath(session_manifest),
            "pid_file": os.path.abspath(pid_path),
            "pid": int(pid) if pid else None,
            "port": int(port),
            "candidate_sha": str(candidate_sha or ""),
            "baseline_sha": str(baseline_sha or ""),
            "release_validation_session_id": os.environ.get(
                "COVERAGE_SERVING_RELEASE_SESSION_ID", ""
            ),
            "candidate_artifact_sha256": os.environ.get(
                "COVERAGE_SERVING_CANDIDATE_ARTIFACT_SHA256", ""
            ),
            "served_root_sha256": os.environ.get(
                "COVERAGE_SERVING_SERVED_ROOT_SHA256", ""
            ),
            "updated_at": time.time(),
        }
        payload.update(extra)
        _atomic_write_json(serving_state_path, payload)

    if os.path.isfile(pid_path):
        try:
            existing_pid = _read_pid(pid_path)
            if _pid_exists(existing_pid):
                owned = (existing_pid in (session.data.get("pids") or []) and
                         session._owned_pid(existing_pid))
                if not owned:
                    raise RuntimeError(
                        "existing staging process is not owned by validation session"
                    )
                record_serving_state("ACTIVE", existing_pid)
                return
        except (OSError, ValueError):
            pass
    env = dict(os.environ)
    env["COVERAGE_CONFIG_PATH"] = os.path.abspath(config_path)
    log_path = pid_path + ".log"
    os.makedirs(os.path.dirname(os.path.abspath(pid_path)), exist_ok=True)
    log_stream = open(log_path, "ab")
    process = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "enhance_coverage.py"), "server"],
        cwd=ROOT, env=env, stdout=log_stream, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    session.add_process(process.pid, port=port, listener={
        "host": host, "port": port, "pid": process.pid,
    })
    with open(pid_path, "w", encoding="utf-8") as stream:
        stream.write(str(process.pid))
    log_stream.close()
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(endpoint, timeout=1) as response:
                if response.status == 200:
                    record_serving_state("ACTIVE", process.pid)
                    return
        except Exception:
            time.sleep(0.25)
    teardown = session.teardown(evidence_path=session_manifest + ".teardown.json")
    record_serving_state(
        "STOPPED", process.pid, startup_status="FAILED", teardown=teardown
    )
    raise RuntimeError("staging API did not become ready; see {}".format(log_path))


def stop(pid_path, session_manifest="", evidence_path="", serving=False,
         serving_state_path=""):
    session_prefix = "COVERAGE_SERVING" if serving else "COVERAGE_VALIDATION"
    fallback_prefix = "COVERAGE_VALIDATION" if serving else "COVERAGE_SERVING"
    session_manifest = session_manifest or os.environ.get(
        session_prefix + "_SESSION_MANIFEST", ""
    ) or os.environ.get(
        fallback_prefix + "_SESSION_MANIFEST", ""
    ) or (pid_path + ".session.json")
    evidence_path = evidence_path or os.environ.get(
        session_prefix + "_TEARDOWN_EVIDENCE", ""
    ) or os.environ.get(
        fallback_prefix + "_TEARDOWN_EVIDENCE", ""
    )
    if os.path.isfile(session_manifest):
        session = ValidationSession.load(session_manifest)
        result = session.teardown(
            evidence_path=evidence_path or session_manifest + ".teardown.json"
        )
        if serving:
            serving_state_path = serving_state_path or os.environ.get(
                "COVERAGE_SERVING_STATE_PATH", ""
            ) or (pid_path + ".current.json")
            state = _load_json(serving_state_path) or {}
            if result.get("status") == "PASSED":
                state.update({
                    "schema_version": 1,
                    "role": "production_serving",
                    "status": "STOPPED",
                    "session_id": session.data.get("session_id", ""),
                    "session_manifest": os.path.abspath(session_manifest),
                    "pid_file": os.path.abspath(pid_path),
                    "pid": state.get("pid"),
                    "stopped_at": time.time(),
                    "teardown": result,
                })
                _atomic_write_json(serving_state_path, state)
            elif state:
                state.update({"status": "ACTIVE", "stop_failure": result})
                _atomic_write_json(serving_state_path, state)
        return result
    if not os.path.isfile(pid_path):
        return {"status": "PASSED", "pids_closed": True, "ports_closed": True}
    # A PID file without a session manifest has no safe ownership proof.  Do
    # not signal a potentially unrelated or PID-reused process.
    return {
        "status": "FAILED", "pids_closed": False, "ports_closed": False,
        "reason": "validation session manifest is required to stop a process",
    }


def stop_current(state_path, fallback_pid_path, fallback_session_manifest="",
                 fallback_evidence_path=""):
    """Stop the stable CURRENT owner or the explicit legacy baseline.

    The fallback is only for the one-time pre-immutable baseline.  Once a
    serving state exists, an invalid ownership record is a hard failure rather
    than permission to guess at another PID file.
    """
    state_file_exists = bool(state_path and os.path.lexists(os.path.abspath(state_path)))
    state = _load_json(state_path) if state_file_exists else None
    if state_file_exists and state is None:
        return {
            "status": "FAILED", "pids_closed": False, "ports_closed": False,
            "reason": "current serving state is not valid JSON",
        }
    if state and state.get("status") == "ACTIVE":
        if state.get("role") != "production_serving" or not state.get("session_id"):
            return {
                "status": "FAILED", "pids_closed": False, "ports_closed": False,
                "reason": "current serving state identity is invalid",
            }
        pid_path = state.get("pid_file") or fallback_pid_path
        manifest_path = state.get("session_manifest") or ""
        if not manifest_path or not pid_path:
            return {
                "status": "FAILED", "pids_closed": False, "ports_closed": False,
                "reason": "current serving ownership manifest is required",
            }
        return stop(
            pid_path, manifest_path, evidence_path=fallback_evidence_path,
            serving=True, serving_state_path=state_path,
        )
    return stop(
        fallback_pid_path, fallback_session_manifest,
        evidence_path=fallback_evidence_path, serving=False,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=("start", "stop", "stop-current", "freeze", "drain", "open")
    )
    parser.add_argument("--serving", action="store_true")
    parser.add_argument("--config", required=True)
    parser.add_argument("--pid-file", required=True)
    parser.add_argument("--endpoint", default="http://127.0.0.1:19528/api/coverage/release")
    parser.add_argument("--session-manifest", default="")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--candidate-sha", default="")
    parser.add_argument("--baseline-sha", default="")
    parser.add_argument("--allow-non-loopback", action="store_true")
    parser.add_argument("--allowlist", action="append", default=[])
    parser.add_argument("--temporary-token", default="")
    parser.add_argument("--expires-at", default="")
    parser.add_argument("--evidence", default="")
    parser.add_argument("--state-file", default="")
    args = parser.parse_args()
    if args.action == "start":
        start(
            args.config, args.pid_file, args.endpoint,
            session_manifest=args.session_manifest,
            session_id=args.session_id,
            candidate_sha=args.candidate_sha,
            baseline_sha=args.baseline_sha,
            allow_non_loopback=args.allow_non_loopback,
            allowlist=args.allowlist,
            temporary_token=args.temporary_token,
            expires_at=args.expires_at,
            serving=args.serving,
            serving_state_path=args.state_file,
        )
    elif args.action == "stop":
        result = stop(
            args.pid_file, args.session_manifest, args.evidence,
            serving=args.serving, serving_state_path=args.state_file,
        )
        if result and result.get("status") != "PASSED":
            raise SystemExit(1)
    elif args.action == "stop-current":
        result = stop_current(
            args.state_file, args.pid_file, args.session_manifest, args.evidence
        )
        if result and result.get("status") != "PASSED":
            raise SystemExit(1)
    # freeze/open are enforced by the shared marker in UpgradeLifecycle;
    # these commands are explicit lifecycle acknowledgements.
    elif args.action in ("freeze", "drain", "open"):
        return


if __name__ == "__main__":
    main()
