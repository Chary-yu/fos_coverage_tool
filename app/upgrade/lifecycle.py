"""Explicit traffic/write lifecycle for a safe upgrade.

The freeze marker is shared with the HTTP runtime, so a cutover cannot merely
log that writes are frozen while the API continues accepting mutations.
External stop/start/open commands are deliberately explicit: an absent
command is an unavailable lifecycle capability, never an implicit success.
"""

import json
import os
import shlex
import subprocess
import time
import urllib.request
from typing import Any, Dict, Optional


def runtime_state_root(repo_root: str, config: Dict[str, Any]) -> str:
    state = config.get("runtime_state") or {}
    root = state.get("root") or os.path.join(repo_root, ".runtime-state")
    if not os.path.isabs(root):
        root = os.path.join(repo_root, root)
    return os.path.realpath(root)


def freeze_marker_path(repo_root: str, config: Dict[str, Any]) -> str:
    return os.path.join(runtime_state_root(repo_root, config), "upgrade-writes-frozen.json")


def writes_are_frozen(repo_root: str, config: Dict[str, Any]) -> bool:
    return os.path.isfile(freeze_marker_path(repo_root, config))


class UpgradeLifecycle:
    def __init__(self, repo_root: str, config: Optional[Dict[str, Any]], mode: str,
                 previous_release: Optional[Dict[str, Any]] = None):
        self.repo_root = os.path.realpath(repo_root)
        self.config = dict(config or {})
        self.mode = mode
        self.root = runtime_state_root(self.repo_root, self.config)
        self.marker = freeze_marker_path(self.repo_root, self.config)
        self.active = False
        self.api_started = False
        self.previous_release = dict(previous_release or {})

    def _commands(self) -> Dict[str, Any]:
        return dict((self.config.get("upgrade") or {}).get("commands") or {})

    def _run_command(self, name: str) -> Dict[str, Any]:
        command = self._commands().get(name)
        if not command:
            raise RuntimeError("upgrade lifecycle command '{}' is not configured".format(name))
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not argv or any(not isinstance(part, str) for part in argv):
            raise RuntimeError("upgrade lifecycle command '{}' is invalid".format(name))
        started = time.time()
        result = subprocess.run(argv, cwd=self.repo_root, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=False)
        return {
            "name": name,
            "command": argv,
            "exit_code": result.returncode,
            "stdout": result.stdout.decode("utf-8", errors="replace")[-4000:],
            "stderr": result.stderr.decode("utf-8", errors="replace")[-4000:],
            "duration_ms": round((time.time() - started) * 1000, 2),
            "status": "PASSED" if result.returncode == 0 else "FAILED",
        }

    def freeze(self, revision: str) -> Dict[str, Any]:
        os.makedirs(self.root, exist_ok=True)
        payload = {
            "revision": revision,
            "mode": self.mode,
            "created_at": time.time(),
            "reason": "upgrade_cutover",
        }
        temp = self.marker + ".part"
        with open(temp, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            try:
                os.fsync(stream.fileno())
            except OSError:
                pass
        os.replace(temp, self.marker)
        self.active = True
        command_result = self._run_command("freeze_traffic")
        if command_result["status"] != "PASSED":
            self.abort()
            raise RuntimeError("freeze_traffic failed")
        return {"status": "PASSED", "evidence_class": "staging_cutover" if self.mode == "staging" else "production_cutover", "marker": self.marker, "command": command_result}

    def drain(self, connection, timeout_sec: float = 30.0) -> Dict[str, Any]:
        deadline = time.time() + float(timeout_sec)
        last_count = None
        while time.time() <= deadline:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS active_count FROM coverage_background_jobs WHERE state IN ('queued', 'running')")
                row = cursor.fetchone() or {"active_count": 0}
            last_count = int(row.get("active_count", 0) if isinstance(row, dict) else row[0])
            if last_count == 0:
                command_result = self._run_command("drain_jobs")
                if command_result["status"] != "PASSED":
                    raise RuntimeError("drain_jobs failed")
                return {"status": "PASSED", "evidence_class": "staging_cutover" if self.mode == "staging" else "production_cutover", "active_jobs": 0, "command": command_result}
            time.sleep(0.25)
        raise RuntimeError("background jobs did not drain: {} still active".format(last_count))

    def stop_api(self) -> Dict[str, Any]:
        result = self._run_command("stop_api")
        if result["status"] != "PASSED":
            raise RuntimeError("stop_api failed")
        self.api_started = False
        return result

    def start_api(self) -> Dict[str, Any]:
        # Treat a failed start as potentially partially started; abort must
        # still attempt the stop command rather than leaving a candidate PID.
        self.api_started = True
        result = self._run_command("start_api")
        if result["status"] != "PASSED":
            raise RuntimeError("start_api failed")
        return result

    def open_traffic(self) -> Dict[str, Any]:
        result = self._run_command("open_traffic")
        if result["status"] != "PASSED":
            raise RuntimeError("open_traffic failed")
        self.active = False
        self.api_started = False
        try:
            os.remove(self.marker)
        except FileNotFoundError:
            pass
        return result

    def abort(self) -> Dict[str, Any]:
        evidence_class = "staging_cutover" if self.mode == "staging" else "production_cutover"
        results = []
        # Keep the marker in place while candidate is stopped and the previous
        # release is restored; removing it first would create a write window.
        if self.api_started:
            stop_result = self._run_command("stop_api")
            results.append(stop_result)
            if stop_result["status"] != "PASSED":
                raise RuntimeError("candidate API stop failed during rollback")
            self.api_started = False

        if not self._commands().get("start_previous_api"):
            raise RuntimeError("upgrade lifecycle command 'start_previous_api' is not configured")
        previous_result = self._run_command("start_previous_api")
        results.append(previous_result)
        if previous_result["status"] != "PASSED":
            raise RuntimeError("previous API start failed during rollback")

        endpoint = (self.config.get("upgrade") or {}).get("previous_release_endpoint")
        if endpoint:
            if not self.previous_release:
                raise RuntimeError("previous release identity is required for rollback endpoint verification")
            with urllib.request.urlopen(endpoint, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            actual = payload.get("release") if isinstance(payload, dict) else None
            if not isinstance(actual, dict):
                raise RuntimeError("previous release endpoint returned no release identity")
            for key in ("version", "commit_sha", "build_id", "asset_hash", "schema_version"):
                if actual.get(key) != self.previous_release.get(key):
                    raise RuntimeError("previous release endpoint mismatch: {}".format(key))

        try:
            os.remove(self.marker)
        except FileNotFoundError:
            pass
        self.active = False
        return {
            "status": "PASSED",
            "evidence_class": evidence_class,
            "command": results,
            "exit_code": 0,
            "previous_release_verified": bool(endpoint),
        }
