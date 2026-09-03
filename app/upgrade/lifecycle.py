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
        self.serving_api_started = False
        self.current_serving_managed = False
        self.current_api_stopped = False
        self.current_api_stop_attempted = False
        self.traffic_opened = False
        self.previous_release = dict(previous_release or {})

    def _commands(self) -> Dict[str, Any]:
        return dict((self.config.get("upgrade") or {}).get("commands") or {})

    def _run_command(self, name: str, clear_control_session: bool = False,
                     extra_env: Optional[Dict[str, str]] = None,
                     use_candidate_runtime: bool = True) -> Dict[str, Any]:
        command = self._commands().get(name)
        if not command:
            raise RuntimeError("upgrade lifecycle command '{}' is not configured".format(name))
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not argv or any(not isinstance(part, str) for part in argv):
            raise RuntimeError("upgrade lifecycle command '{}' is invalid".format(name))
        started = time.time()
        command_env = dict(os.environ)
        if clear_control_session:
            for key in (
                    "COVERAGE_VALIDATION_SESSION_MANIFEST",
                    "COVERAGE_VALIDATION_SESSION_ID",
                    "COVERAGE_VALIDATION_CANDIDATE_SHA",
                    "COVERAGE_VALIDATION_BASELINE_SHA",
                    "COVERAGE_VALIDATION_TEARDOWN_EVIDENCE",
                    "COVERAGE_SERVING_SESSION_MANIFEST",
                    "COVERAGE_SERVING_SESSION_ID",
                    "COVERAGE_SERVING_CANDIDATE_SHA",
                    "COVERAGE_SERVING_BASELINE_SHA",
                    "COVERAGE_SERVING_TEARDOWN_EVIDENCE",
                    "COVERAGE_SERVING_RELEASE_SESSION_ID",
                    "COVERAGE_SERVING_CANDIDATE_ARTIFACT_SHA256",
                    "COVERAGE_SERVING_SERVED_ROOT_SHA256",
            ):
                command_env.pop(key, None)
        # A child process must never inherit a candidate DB binding by
        # accident.  Candidate starts opt in below; rollback starts pass
        # ``use_candidate_runtime=False`` and may explicitly bind the old DB.
        command_env.pop("COVERAGE_CANDIDATE_MYSQL_JSON", None)
        if use_candidate_runtime and name in ("start_validation_api", "start_serving_api"):
            candidate_mysql = ((self.config.get("upgrade") or {}).get(
                "candidate_runtime_mysql"
            ) or {})
            if candidate_mysql:
                command_env["COVERAGE_CANDIDATE_MYSQL_JSON"] = json.dumps(
                    candidate_mysql, ensure_ascii=False, sort_keys=True
                )
        if extra_env:
            command_env.update({str(key): str(value) for key, value in extra_env.items()})
        result = subprocess.run(argv, cwd=self.repo_root, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=False, env=command_env)
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

    def stop_validation_api(self) -> Dict[str, Any]:
        result = self._run_command("stop_validation_api")
        if result["status"] != "PASSED":
            raise RuntimeError("stop_validation_api failed")
        self.api_started = False
        return result

    def stop_api(self) -> Dict[str, Any]:
        """Backward-compatible alias for callers outside the release path."""
        command = "stop_validation_api" if self._commands().get("stop_validation_api") else "stop_api"
        result = self._run_command(command)
        if result["status"] != "PASSED":
            raise RuntimeError("{} failed".format(command))
        self.api_started = False
        return result

    def _has_active_current_serving_state(self) -> bool:
        configured = (self.config.get("upgrade") or {}).get("current_serving_state_path")
        if not configured:
            return False
        path = str(configured)
        if not os.path.isabs(path):
            path = os.path.join(self.repo_root, path)
        try:
            with open(os.path.realpath(path), "r", encoding="utf-8") as stream:
                state = json.load(stream)
        except (OSError, ValueError, TypeError):
            return False
        return bool(
            isinstance(state, dict) and
            state.get("status") == "ACTIVE" and
            state.get("role") == "production_serving" and
            state.get("session_id") and
            state.get("pid_file")
        )

    def stop_current_api(self) -> Dict[str, Any]:
        """Stop the stable CURRENT serving process, never the validation API."""
        self.current_serving_managed = self._has_active_current_serving_state()
        self.current_api_stop_attempted = True
        result = self._run_command("stop_current_api", clear_control_session=True)
        if result["status"] != "PASSED":
            raise RuntimeError("stop_current_api failed")
        self.current_api_stopped = True
        result["managed_serving_before_stop"] = self.current_serving_managed
        return result

    def start_validation_api(self) -> Dict[str, Any]:
        # Treat a failed start as potentially partially started; abort must
        # still attempt the stop command rather than leaving a candidate PID.
        self.api_started = True
        result = self._run_command("start_validation_api")
        if result["status"] != "PASSED":
            raise RuntimeError("start_validation_api failed")
        return result

    def start_api(self) -> Dict[str, Any]:
        """Backward-compatible alias for callers outside the release path."""
        command = "start_validation_api" if self._commands().get("start_validation_api") else "start_api"
        self.api_started = True
        result = self._run_command(command)
        if result["status"] != "PASSED":
            raise RuntimeError("{} failed".format(command))
        return result

    def start_serving_api(self) -> Dict[str, Any]:
        """Start the final serving process outside the validation session."""
        self.serving_api_started = True
        result = self._run_command("start_serving_api")
        if result["status"] != "PASSED":
            raise RuntimeError("start_serving_api failed")
        return result

    def stop_serving_api(self) -> Dict[str, Any]:
        """Stop the process currently bound to the immutable serving release."""
        result = self._run_command("stop_serving_api", clear_control_session=True)
        if result["status"] != "PASSED":
            raise RuntimeError("stop_serving_api failed")
        self.serving_api_started = False
        return result

    def open_traffic(self) -> Dict[str, Any]:
        result = self._run_command("open_traffic")
        if result["status"] != "PASSED":
            raise RuntimeError("open_traffic failed")
        # The traffic command may make the release externally reachable, but
        # the upgrade remains rollback-capable until the post-open liveness,
        # exact release identity and health checks pass.  The caller must
        # explicitly finalize the open after that gate.
        self.traffic_opened = True
        return result

    def finalize_traffic_open(self) -> Dict[str, Any]:
        """Commit a traffic open only after all post-open gates pass."""
        if not self.traffic_opened:
            raise RuntimeError("traffic has not been opened")
        self.active = False
        try:
            os.remove(self.marker)
        except FileNotFoundError:
            pass
        return {
            "status": "PASSED",
            "evidence_class": "staging_cutover" if self.mode == "staging" else "production_cutover",
            "command": "UpgradeLifecycle.finalize_traffic_open",
            "exit_code": 0,
        }

    def abort(self) -> Dict[str, Any]:
        evidence_class = "staging_cutover" if self.mode == "staging" else "production_cutover"
        results = []
        # Keep the marker in place while candidate is stopped and the previous
        # release is restored; removing it first would create a write window.
        if self.traffic_opened:
            freeze_result = self._run_command("freeze_traffic")
            results.append(freeze_result)
            if freeze_result["status"] != "PASSED":
                raise RuntimeError("traffic re-freeze failed during rollback")
            self.traffic_opened = False

        # Before the blue/green target has passed, the stable process is still
        # serving the old database.  A failed backup/target/migration must
        # preserve that process; starting a second "previous" API here would
        # create a duplicate owner and would not be a rollback.
        if not self.current_api_stopped and not self.serving_api_started:
            if self.current_api_stop_attempted:
                raise RuntimeError(
                    "current API stop outcome is unknown; refusing automatic rollback"
                )
            if self.active or os.path.isfile(self.marker):
                if not self._commands().get("open_traffic"):
                    raise RuntimeError(
                        "upgrade lifecycle command 'open_traffic' is not configured"
                    )
                reopen_result = self._run_command(
                    "open_traffic", clear_control_session=True,
                    use_candidate_runtime=False,
                )
                results.append(reopen_result)
                if reopen_result["status"] != "PASSED":
                    raise RuntimeError("traffic reopen failed while preserving current API")
                self.traffic_opened = False
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
                "previous_release_verified": False,
                "current_process_preserved": True,
                "restore_endpoint": "",
                "restore_endpoint_key": "",
            }

        restore_managed_serving = self.serving_api_started or self.current_serving_managed

        if self.serving_api_started:
            stop_serving_result = self._run_command("stop_serving_api")
            results.append(stop_serving_result)
            if stop_serving_result["status"] != "PASSED":
                raise RuntimeError("final serving API stop failed during rollback")
            self.serving_api_started = False

        if self.api_started:
            stop_result = self._run_command("stop_validation_api")
            results.append(stop_result)
            if stop_result["status"] != "PASSED":
                raise RuntimeError("validation API stop failed during rollback")
            self.api_started = False

        if restore_managed_serving:
            if not self._commands().get("start_serving_api"):
                raise RuntimeError("upgrade lifecycle command 'start_serving_api' is not configured")
            restore_env = {}
            if self.previous_release.get("commit_sha"):
                restore_env["COVERAGE_SERVING_CANDIDATE_SHA"] = self.previous_release.get("commit_sha")
                restore_env["COVERAGE_SERVING_BASELINE_SHA"] = self.previous_release.get("commit_sha")
            if self.previous_release.get("_published_session_id"):
                restore_env["COVERAGE_SERVING_RELEASE_SESSION_ID"] = self.previous_release.get(
                    "_published_session_id"
                )
            previous_mysql = self.previous_release.get(
                "_previous_runtime_mysql"
            ) or self.config.get("mysql") or {}
            if previous_mysql:
                restore_env["COVERAGE_CANDIDATE_MYSQL_JSON"] = json.dumps(
                    previous_mysql, ensure_ascii=False, sort_keys=True
                )
            previous_result = self._run_command(
                "start_serving_api", clear_control_session=True,
                extra_env=restore_env, use_candidate_runtime=False,
            )
            previous_result["process_role"] = "production_serving_restored"
        else:
            if not self._commands().get("start_previous_api"):
                raise RuntimeError("upgrade lifecycle command 'start_previous_api' is not configured")
            previous_result = self._run_command("start_previous_api", clear_control_session=True)
            previous_result["process_role"] = "previous_release"
        results.append(previous_result)
        if previous_result["status"] != "PASSED":
            raise RuntimeError("previous API start failed during rollback")

        # A managed CURRENT is restored by start_serving_api on the serving
        # endpoint (the standard staging port is 19528).  Only a legacy
        # fallback started by start_previous_api belongs on the previous
        # release endpoint (the standard baseline port is 9528).
        endpoint_key = "release_endpoint" if restore_managed_serving \
            else "previous_release_endpoint"
        endpoint = (self.config.get("upgrade") or {}).get(endpoint_key)
        if endpoint:
            if not self.previous_release:
                raise RuntimeError("previous release identity is required for rollback endpoint verification")
            with urllib.request.urlopen(endpoint, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            actual = payload.get("release") if isinstance(payload, dict) else None
            if not isinstance(actual, dict):
                raise RuntimeError("{} returned no release identity".format(endpoint_key))
            for key in (
                    "version", "commit_sha", "build_id", "asset_hash", "schema_version",
                    "asset_manifest_version", "asset_count", "asset_manifest_hash",
                    "asset_manifest"):
                if actual.get(key) != self.previous_release.get(key):
                    raise RuntimeError("{} mismatch: {}".format(endpoint_key, key))

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
            "restore_endpoint": endpoint,
            "restore_endpoint_key": endpoint_key,
        }
