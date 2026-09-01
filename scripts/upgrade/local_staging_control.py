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
                           allowlist, temporary_token, expires_at):
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
          expires_at="", serving=False):
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
        expires_at,
    )
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
                    return
        except Exception:
            time.sleep(0.25)
    session.teardown(evidence_path=session_manifest + ".teardown.json")
    raise RuntimeError("staging API did not become ready; see {}".format(log_path))


def stop(pid_path, session_manifest="", evidence_path="", serving=False):
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
        return session.teardown(
            evidence_path=evidence_path or session_manifest + ".teardown.json"
        )
    if not os.path.isfile(pid_path):
        return {"status": "PASSED", "pids_closed": True, "ports_closed": True}
    # A PID file without a session manifest has no safe ownership proof.  Do
    # not signal a potentially unrelated or PID-reused process.
    return {
        "status": "FAILED", "pids_closed": False, "ports_closed": False,
        "reason": "validation session manifest is required to stop a process",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop", "freeze", "drain", "open"))
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
        )
    elif args.action == "stop":
        result = stop(args.pid_file, args.session_manifest, args.evidence, serving=args.serving)
        if result and result.get("status") != "PASSED":
            raise SystemExit(1)
    # freeze/open are enforced by the shared marker in UpgradeLifecycle;
    # these commands are explicit lifecycle acknowledgements.
    elif args.action in ("freeze", "drain", "open"):
        return


if __name__ == "__main__":
    main()
