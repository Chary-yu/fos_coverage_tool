"""Production lifecycle adapter for the vfoswind systemd/Nginx layout.

The generic staging controller cannot prove which release a persistent
systemd unit or an Nginx alias serves.  This adapter makes those bindings an
explicit production preflight and owns the small set of commands needed after
the immutable ``CURRENT`` switch:

* systemd must execute ``publish_root/CURRENT/app``;
* Nginx must serve ``publish_root/CURRENT/reports``;
* the persistent EnvironmentFile must bind the serving process to the
  selected database;
* daemon-reload, service restart, ``nginx -t``, and Nginx reload are explicit
  and independently evidenced.

Preflight is read-only.  Runtime binding is only called by the Phase-D
cutover path after Candidate validation has reached PRE_CUTOVER_READY.
"""

from __future__ import print_function

import json
import os
import shlex
import subprocess
import tempfile

from app.release_publication import (
    validate_production_application_bundle,
    validate_production_application_root,
)


ADAPTER_NAME = "vfoswind"
_RUNTIME_KEYS = (
    "host", "port", "user", "password", "database", "charset",
    "connect_timeout",
)


def _literal(path):
    return os.path.normpath(os.path.abspath(str(path)))


def _argv(value, field):
    if isinstance(value, str):
        value = shlex.split(value)
    else:
        value = list(value or [])
    if not value or any(not isinstance(item, str) or not item for item in value):
        raise ValueError("production integration {} command is invalid".format(field))
    return value


def _unquote_environment_value(value):
    value = str(value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        quote = value[0]
        value = value[1:-1]
        if quote == '"':
            value = value.replace('\\"', '"').replace('\\\\', '\\')
    return value


def parse_environment_file(text):
    """Parse the small KEY=VALUE subset used by systemd EnvironmentFile."""
    values = {}
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key:
            values[key] = _unquote_environment_value(value)
    return values


def _quote_environment_value(value):
    # systemd EnvironmentFile accepts a double-quoted value.  Escape the
    # characters that can otherwise be interpreted while preserving JSON.
    value = str(value)
    return '"{}"'.format(
        value.replace("\\", "\\\\").replace('"', '\\"')
    )


def _runtime_mysql_config(target):
    target = dict(target or {})
    if isinstance(target.get("mysql"), dict):
        target = dict(target["mysql"])
    database = str(target.get("database") or "").strip()
    if not database:
        raise ValueError("production Candidate database binding is missing database")
    user = str(target.get("user") or "").strip()
    if not user:
        raise ValueError("production Candidate database binding is missing application user")
    if user.lower() == "root":
        raise ValueError("production Candidate database binding may not use root")
    result = {}
    for key in _RUNTIME_KEYS:
        if key in target and target.get(key) is not None:
            result[key] = target[key]
    result["database"] = database
    return result


class VfoswindProductionLifecycle(object):
    """Validate and apply the vfoswind persistent serving bindings."""

    def __init__(self, publish_root, config, command_runner=None):
        self.publish_root = _literal(publish_root)
        self.config = dict(config or {})
        self.command_runner = command_runner or self._default_command_runner
        self.current_app = _literal(os.path.join(
            self.publish_root, "CURRENT", "app"
        ))
        self.current_reports = _literal(os.path.join(
            self.publish_root, "CURRENT", "reports"
        ))
        self.environment_file = _literal(
            self.config.get("runtime_environment_file") or
            self.config.get("environment_file") or ""
        ) if (self.config.get("runtime_environment_file") or
              self.config.get("environment_file")) else ""
        self.validation_unit = str(
            self.config.get("validation_systemd_unit") or
            self.config.get("validation_unit") or ""
        ).strip()
        self.validation_unit_file = _literal(
            self.config.get("validation_systemd_unit_file") or
            self.config.get("validation_unit_file") or ""
        ) if (self.config.get("validation_systemd_unit_file") or
              self.config.get("validation_unit_file")) else ""
        self.validation_environment_file = _literal(
            self.config.get("validation_runtime_environment_file") or
            self.config.get("validation_environment_file") or ""
        ) if (self.config.get("validation_runtime_environment_file") or
              self.config.get("validation_environment_file")) else ""
        self.validation_config_path = _literal(
            self.config.get("validation_config_path") or ""
        ) if self.config.get("validation_config_path") else ""
        self._previous_environment = None
        self._previous_environment_mode = None
        self._previous_validation_environment = None
        self._previous_validation_environment_mode = None
        self._previous_validation_unit = None
        self._previous_validation_unit_mode = None
        self._previous_unit = None
        self._previous_unit_mode = None
        self._previous_nginx = None
        self._previous_nginx_mode = None
        self.release_bindings_changed = False
        self.validation_binding_changed = False
        self.runtime_bound = False

    @staticmethod
    def _default_command_runner(argv):
        return subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
        )

    def _read_file(self, path, field):
        if not path or not os.path.isfile(path) or os.path.islink(path):
            raise RuntimeError(
                "production integration {} must be a regular file: {}".format(
                    field, path
                )
            )
        try:
            with open(path, "r", encoding="utf-8") as stream:
                return stream.read()
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(
                "production integration {} is unreadable: {}".format(field, exc)
            )

    def _run(self, field, required=True):
        commands = self.config.get("commands") or {}
        value = commands.get(field)
        if not value:
            if required:
                raise RuntimeError(
                    "production integration command '{}' is required".format(field)
                )
            return None
        argv = _argv(value, field)
        result = self.command_runner(argv)
        return {
            "name": field,
            "command": argv,
            "status": "PASSED" if result.returncode == 0 else "FAILED",
            "exit_code": result.returncode,
            "stdout": result.stdout.decode("utf-8", errors="replace")[-2000:],
            "stderr": result.stderr.decode("utf-8", errors="replace")[-2000:],
        }

    def _unit_definition(self):
        configured = self.config.get("systemd_unit_file") or \
            self.config.get("unit_file")
        if configured:
            return self._read_file(_literal(configured), "systemd_unit_file")
        unit = str(self.config.get("systemd_unit") or "").strip()
        if not unit:
            raise RuntimeError("production integration systemd_unit is required")
        result = self.command_runner(["systemctl", "cat", unit])
        if result.returncode != 0:
            raise RuntimeError(
                "systemd unit definition probe failed: {}".format(
                    result.stderr.decode("utf-8", errors="replace")
                )
            )
        return result.stdout.decode("utf-8", errors="replace")

    def _validation_unit_definition(self):
        if self.validation_unit_file:
            return self._read_file(
                self.validation_unit_file, "validation_systemd_unit_file"
            )
        if not self.validation_unit:
            raise RuntimeError(
                "production integration validation_systemd_unit is required"
            )
        result = self.command_runner(
            ["systemctl", "cat", self.validation_unit]
        )
        if result.returncode != 0:
            raise RuntimeError(
                "validation systemd unit definition probe failed: {}".format(
                    result.stderr.decode("utf-8", errors="replace")
                )
            )
        return result.stdout.decode("utf-8", errors="replace")

    def _unit_file_path(self):
        configured = self.config.get("systemd_unit_file") or \
            self.config.get("unit_file")
        return _literal(configured) if configured else ""

    def _validation_unit_file_path(self):
        return self.validation_unit_file

    def _nginx_file_path(self):
        configured = self.config.get("nginx_config_path") or \
            self.config.get("nginx_config")
        return _literal(configured) if configured else ""

    @staticmethod
    def _contains_alias(text, alias_root):
        alias = "alias {}/;".format(alias_root.rstrip(os.sep))
        return text.count(alias) == 1

    @staticmethod
    def _replace_atomic(path, content, mode):
        directory = os.path.dirname(path) or "."
        descriptor, temporary = tempfile.mkstemp(
            prefix=".coverage-production-binding-", dir=directory
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                try:
                    os.fsync(stream.fileno())
                except OSError:
                    pass
            if mode is not None:
                os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            try:
                os.remove(temporary)
            except OSError:
                pass

    def _read_binding_file(self, path, field):
        if not path:
            raise RuntimeError(
                "production integration {} is required for binding transition".format(
                    field
                )
            )
        text = self._read_file(path, field)
        return text, os.stat(path).st_mode & 0o777

    def _validate_commands(self, validation_commands=None):
        unit = str(self.config.get("systemd_unit") or "").strip()
        if not unit:
            raise RuntimeError("production integration systemd_unit is required")
        commands = self.config.get("commands") or {}
        for field in (
                "daemon_reload", "service_stop", "service_restart",
                "nginx_test", "nginx_reload"):
            _argv(commands.get(field), field)
        stop = _argv(commands.get("service_stop"), "service_stop")
        restart = _argv(commands.get("service_restart"), "service_restart")
        if unit not in stop or unit not in restart:
            raise RuntimeError(
                "production service commands must bind systemd unit {}".format(unit)
            )
        nginx_config = self.config.get("nginx_config_path") or \
            self.config.get("nginx_config")
        if not nginx_config:
            raise RuntimeError("production integration nginx_config_path is required")
        if not self.environment_file:
            raise RuntimeError(
                "production integration runtime_environment_file is required"
            )
        if not self.validation_unit:
            raise RuntimeError(
                "production integration validation_systemd_unit is required"
            )
        if self.validation_unit == unit:
            raise RuntimeError(
                "validation systemd unit must be separate from the serving unit"
            )
        if not self.validation_unit_file:
            raise RuntimeError(
                "production integration validation_systemd_unit_file is required"
            )
        if not self.validation_environment_file:
            raise RuntimeError(
                "production integration validation_runtime_environment_file is required"
            )
        if not self.validation_config_path:
            raise RuntimeError(
                "production integration validation_config_path is required"
            )
        if self.validation_unit_file == self._unit_file_path():
            raise RuntimeError(
                "validation systemd unit file must be separate from the serving unit"
            )
        if self.validation_environment_file == self.environment_file:
            raise RuntimeError(
                "validation EnvironmentFile must be separate from the serving binding"
            )
        if validation_commands:
            validation_start = _argv(
                validation_commands.get("start_validation_api"),
                "start_validation_api",
            )
            validation_stop = _argv(
                validation_commands.get("stop_validation_api"),
                "stop_validation_api",
            )
            if self.validation_unit not in validation_start or \
                    self.validation_unit not in validation_stop:
                raise RuntimeError(
                    "validation lifecycle commands must bind systemd unit {}".format(
                        self.validation_unit
                    )
                )

    @staticmethod
    def _validate_runtime_unit(text, application_root, environment_file, label):
        application_root = _literal(application_root)
        expected_environment = "EnvironmentFile={}".format(environment_file)
        if expected_environment not in text and \
                "EnvironmentFile=-{}".format(environment_file) not in text:
            raise RuntimeError(
                "{} systemd unit does not persist its EnvironmentFile".format(label)
            )
        if "WorkingDirectory={}".format(application_root) not in text:
            raise RuntimeError(
                "{} systemd WorkingDirectory is not bound to the expected application root".format(label)
            )
        exec_lines = [
            line.strip() for line in text.splitlines()
            if line.strip().startswith("ExecStart=")
        ]
        if not any(
                application_root in line and "enhance_coverage.py" in line
                for line in exec_lines
        ):
            raise RuntimeError(
                "{} systemd ExecStart is not bound to the expected application root".format(label)
            )

    def _read_runtime_mysql_binding(self, path, field):
        text = self._read_file(path, field)
        values = parse_environment_file(text)
        mysql_json = values.get("COVERAGE_MYSQL_JSON")
        if mysql_json:
            try:
                runtime = _runtime_mysql_config(json.loads(mysql_json))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "COVERAGE_MYSQL_JSON in {} is invalid: {}".format(field, exc)
                )
        elif values.get("COVERAGE_CONFIG_PATH"):
            config_path = values["COVERAGE_CONFIG_PATH"]
            if not os.path.isabs(config_path):
                config_path = os.path.join(os.path.dirname(path), config_path)
            try:
                with open(config_path, "r", encoding="utf-8") as stream:
                    runtime = _runtime_mysql_config(json.load(stream))
            except (OSError, ValueError, TypeError) as exc:
                raise RuntimeError(
                    "persistent database config in {} is invalid: {}".format(
                        field, exc
                    )
                )
        else:
            raise RuntimeError(
                "{} must bind COVERAGE_MYSQL_JSON or COVERAGE_CONFIG_PATH".format(field)
            )
        return text, runtime

    def preflight(self, expected_database="", candidate_application_root="",
                  candidate_ports=None, validation_commands=None):
        """Read and validate systemd, Nginx, and persistent DB bindings."""
        if str(self.config.get("adapter") or ADAPTER_NAME).strip() != ADAPTER_NAME:
            raise RuntimeError("unsupported production lifecycle adapter")
        self._validate_commands(validation_commands=validation_commands)
        unit_text = self._unit_definition()
        expected_environment = "EnvironmentFile={}".format(self.environment_file)
        if expected_environment not in unit_text and \
                ("EnvironmentFile=-{}".format(self.environment_file) not in unit_text):
            raise RuntimeError(
                "systemd unit does not persist the configured EnvironmentFile"
            )
        current_path = os.path.join(self.publish_root, "CURRENT")
        current_exists = os.path.islink(current_path)
        expected_application_root = self.current_app
        application_source = "immutable_current"
        deployment_layout = "IMMUTABLE_CURRENT"
        transition_required = False
        if current_exists:
            application_evidence = validate_production_application_bundle(
                os.path.realpath(current_path)
            )
        else:
            legacy_application_root = self.config.get("legacy_application_root")
            legacy_served_root = self.config.get("legacy_served_root") or \
                self.config.get("legacy_reports_root")
            if not legacy_application_root or not legacy_served_root:
                raise RuntimeError(
                    "Flat vfoswind deployment requires legacy_application_root and legacy_served_root"
                )
            expected_application_root = _literal(legacy_application_root)
            legacy_served_root = _literal(legacy_served_root)
            if not os.path.isdir(legacy_served_root) or os.path.islink(legacy_served_root):
                raise RuntimeError(
                    "Flat vfoswind legacy_served_root must be a real directory"
                )
            application_evidence = validate_production_application_root(
                expected_application_root, require_git_compat_shim=False
            )
            application_source = "legacy_flat_application_root"
            deployment_layout = "FLAT"
            transition_required = True

        self._validate_runtime_unit(
            unit_text, expected_application_root, self.environment_file,
            "production",
        )

        nginx_path = self._nginx_file_path()
        nginx_text = self._read_file(nginx_path, "nginx_config")
        expected_reports_root = self.current_reports
        if transition_required:
            expected_reports_root = _literal(
                self.config.get("legacy_served_root") or
                self.config.get("legacy_reports_root")
            )
        if not self._contains_alias(nginx_text, expected_reports_root):
            raise RuntimeError(
                "Nginx alias is not bound to the expected production reports root"
            )
        expected_proxy = str(self.config.get("nginx_proxy_pass") or "").strip()
        if expected_proxy and expected_proxy not in nginx_text:
            raise RuntimeError("Nginx proxy_pass does not match the production binding")

        environment_text, runtime_config = self._read_runtime_mysql_binding(
            self.environment_file, "runtime_environment_file"
        )
        runtime_database = runtime_config.get("database", "")
        runtime_user = runtime_config.get("user", "")
        if not runtime_user or runtime_user.lower() == "root":
            raise RuntimeError(
                "persistent production database binding must use a non-root application user"
            )
        expected_database = str(expected_database or "").strip()
        if expected_database and runtime_database.lower() != expected_database.lower():
            raise RuntimeError(
                "persistent production database binding does not match the active database"
            )
        validation_application = str(
            candidate_application_root or
            self.config.get("validation_application_root") or ""
        ).strip()
        if not validation_application:
            raise RuntimeError(
                "production integration validation_application_root is required"
            )
        validation_application = _literal(validation_application)
        configured_validation_application = str(
            self.config.get("validation_application_root") or ""
        ).strip()
        if configured_validation_application and \
                validation_application != _literal(configured_validation_application):
            raise RuntimeError(
                "validation_application_root does not match the configured Candidate root"
            )
        validation_application_evidence = validate_production_application_root(
            validation_application
        )
        validation_unit_text = self._validation_unit_definition()
        self._validate_runtime_unit(
            validation_unit_text, validation_application,
            self.validation_environment_file, "validation",
        )
        validation_environment_text, validation_runtime = \
            self._read_runtime_mysql_binding(
                self.validation_environment_file,
                "validation_runtime_environment_file",
            )
        validation_values = parse_environment_file(validation_environment_text)
        configured_validation_config = validation_values.get(
            "COVERAGE_CONFIG_PATH", ""
        )
        if not configured_validation_config:
            raise RuntimeError(
                "validation EnvironmentFile must bind COVERAGE_CONFIG_PATH"
            )
        configured_validation_config = _literal(
            configured_validation_config if os.path.isabs(
                configured_validation_config
            ) else os.path.join(
                os.path.dirname(self.validation_environment_file),
                configured_validation_config,
            )
        )
        if configured_validation_config != self.validation_config_path:
            raise RuntimeError(
                "validation EnvironmentFile config path does not match the integration binding"
            )
        try:
            with open(self.validation_config_path, "r", encoding="utf-8") as stream:
                validation_config = json.load(stream)
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError(
                "validation runtime config is invalid: {}".format(exc)
            )
        if not isinstance(validation_config, dict):
            raise RuntimeError("validation runtime config must be an object")
        if candidate_ports:
            configured_port = int(
                (validation_config.get("server") or {}).get("port") or 0
            )
            expected_ports = set(int(port) for port in candidate_ports)
            if configured_port not in expected_ports:
                raise RuntimeError(
                    "validation runtime config port is not an owned Candidate port"
                )
        validation_user = validation_runtime.get("user", "")
        if not validation_user or validation_user.lower() == "root":
            raise RuntimeError(
                "validation database binding must use a non-root application user"
            )
        return {
            "status": "PASSED",
            "adapter": ADAPTER_NAME,
            "publish_root": self.publish_root,
            "systemd_unit": str(self.config.get("systemd_unit")),
            "systemd_working_directory": expected_application_root,
            "systemd_environment_file": self.environment_file,
            "nginx_config": nginx_path,
            "nginx_alias": expected_reports_root,
            "runtime_database": runtime_database,
            "runtime_application_user": runtime_user,
            "validation_systemd_unit": self.validation_unit,
            "validation_systemd_unit_file": self.validation_unit_file,
            "validation_runtime_environment_file": self.validation_environment_file,
            "validation_application_root": validation_application,
            "validation_application": validation_application_evidence,
            "validation_runtime_database": validation_runtime.get("database", ""),
            "validation_runtime_application_user": validation_user,
            "validation_config_path": self.validation_config_path,
            "application_source": application_source,
            "application": application_evidence,
            "deployment_layout": deployment_layout,
            "transition_required": transition_required,
            "binding_transition": {
                "systemd_unit_file": self._unit_file_path(),
                "nginx_config_path": nginx_path,
                "target_systemd_working_directory": self.current_app,
                "target_nginx_alias": self.current_reports,
            },
            "commands_validated": [
                "daemon_reload", "service_stop", "service_restart",
                "nginx_test", "nginx_reload",
            ],
            "read_only": True,
            "command": "systemd unit + Nginx config + persistent EnvironmentFile preflight",
            "exit_code": 0,
        }

    @staticmethod
    def _render_mysql_environment(text, target_mysql):
        rendered = "COVERAGE_MYSQL_JSON={}".format(
            _quote_environment_value(json.dumps(
                target_mysql, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ))
        )
        lines = text.splitlines(True)
        updated = []
        replaced = False
        for line in lines:
            if line.lstrip().startswith("COVERAGE_MYSQL_JSON="):
                updated.append(rendered + ("\n" if line.endswith("\n") else ""))
                replaced = True
            else:
                updated.append(line)
        if not replaced:
            if updated and not updated[-1].endswith("\n"):
                updated[-1] += "\n"
            updated.append(rendered + "\n")
        return "".join(updated)

    def bind_validation_candidate(self, application_root, target):
        """Bind the isolated validation unit to the immutable Candidate.

        ``systemctl start`` does not inherit the controller's environment.
        Therefore a production validation service gets its own unit and
        EnvironmentFile, both atomically rebound to the prepared release and
        disposable database before the service starts.  The serving unit and
        serving EnvironmentFile are not touched here.
        """
        application_root = _literal(application_root)
        validate_production_application_root(application_root)
        target_mysql = _runtime_mysql_config(target)
        source_application = _literal(
            self.config.get("validation_application_root") or ""
        )
        if not source_application:
            raise RuntimeError(
                "validation_application_root is required for Candidate binding"
            )
        unit_path = self._validation_unit_file_path()
        environment_path = self.validation_environment_file
        unit_text, unit_mode = self._read_binding_file(
            unit_path, "validation_systemd_unit_file"
        )
        environment_text, environment_mode = self._read_binding_file(
            environment_path, "validation_runtime_environment_file"
        )
        self._validate_runtime_unit(
            unit_text, source_application, environment_path, "validation"
        )
        self._previous_validation_unit = unit_text.encode("utf-8")
        self._previous_validation_unit_mode = unit_mode
        self._previous_validation_environment = environment_text.encode("utf-8")
        self._previous_validation_environment_mode = environment_mode
        updated_unit = unit_text.replace(source_application, application_root)
        if updated_unit == unit_text:
            raise RuntimeError(
                "validation systemd unit did not contain the configured Candidate root"
            )
        updated_environment = self._render_mysql_environment(
            environment_text, target_mysql
        )
        try:
            self._replace_atomic(unit_path, updated_unit, unit_mode)
            self._replace_atomic(
                environment_path, updated_environment, environment_mode
            )
            rebound_unit = self._read_file(
                unit_path, "validation_systemd_unit_file"
            )
            rebound_environment, rebound_runtime = self._read_runtime_mysql_binding(
                environment_path, "validation_runtime_environment_file"
            )
            self._validate_runtime_unit(
                rebound_unit, application_root, environment_path, "validation"
            )
            if rebound_runtime.get("database") != target_mysql.get("database") or \
                    rebound_runtime.get("user") != target_mysql.get("user"):
                raise RuntimeError(
                    "validation Candidate database binding did not verify"
                )
        except Exception:
            self.restore_validation_candidate_binding()
            raise
        self.validation_binding_changed = True
        return {
            "status": "PASSED",
            "adapter": ADAPTER_NAME,
            "validation_systemd_unit": self.validation_unit,
            "validation_systemd_unit_file": unit_path,
            "validation_runtime_environment_file": environment_path,
            "validation_application_root": application_root,
            "target_database": target_mysql.get("database"),
            "runtime_application_user": target_mysql.get("user"),
            "command": "atomic validation systemd/Candidate EnvironmentFile binding",
            "credentials_written_to_evidence": False,
            "exit_code": 0,
        }

    def restore_validation_candidate_binding(self):
        """Restore the exact pre-validation unit and database binding."""
        if self._previous_validation_unit is None and \
                self._previous_validation_environment is None:
            return {
                "status": "PASSED", "skipped": True,
                "reason": "no_validation_candidate_binding",
            }
        if self._previous_validation_unit is not None:
            self._replace_atomic(
                self._validation_unit_file_path(),
                self._previous_validation_unit.decode("utf-8"),
                self._previous_validation_unit_mode,
            )
        if self._previous_validation_environment is not None:
            self._replace_atomic(
                self.validation_environment_file,
                self._previous_validation_environment.decode("utf-8"),
                self._previous_validation_environment_mode,
            )
        self.validation_binding_changed = False
        return {
            "status": "PASSED",
            "adapter": ADAPTER_NAME,
            "restored_previous_validation_binding": True,
            "credentials_written_to_evidence": False,
            "command": "atomic validation systemd/EnvironmentFile rollback",
            "exit_code": 0,
        }

    def validation_process_ownership(self):
        """Return the systemd MainPID for session-manifest ownership joining."""
        if not self.validation_unit:
            raise RuntimeError("validation systemd unit is required")
        probe = self.config.get("validation_main_pid_probe") or [
            "systemctl", "show", self.validation_unit,
            "--property=MainPID", "--value",
        ]
        argv = _argv(probe, "validation_main_pid_probe")
        result = self.command_runner(argv)
        if result.returncode != 0:
            raise RuntimeError(
                "validation systemd MainPID probe failed: {}".format(
                    result.stderr.decode("utf-8", errors="replace")
                )
            )
        values = result.stdout.decode("utf-8", errors="replace").split()
        try:
            pid = int(values[-1]) if values else 0
        except (TypeError, ValueError):
            pid = 0
        if pid <= 1:
            raise RuntimeError(
                "validation systemd MainPID probe returned no owned process"
            )
        return {
            "status": "PASSED",
            "validation_systemd_unit": self.validation_unit,
            "pid": pid,
            "command": argv,
            "exit_code": 0,
        }

    def bind_current_release(self):
        """Bind Flat vfoswind files to the immutable CURRENT release.

        The method is called only after Phase-D has stopped the old service
        and switched CURRENT.  Each file is replaced atomically, and the
        original bytes are retained so the controller can restore the exact
        pre-adoption binding if a later cutover gate fails.
        """
        current_path = os.path.join(self.publish_root, "CURRENT")
        if not os.path.islink(current_path):
            raise RuntimeError("vfoswind CURRENT must exist before binding production files")
        validate_production_application_bundle(os.path.realpath(current_path))
        unit_path = self._unit_file_path()
        nginx_path = self._nginx_file_path()
        if not unit_path or not nginx_path:
            raise RuntimeError(
                "vfoswind binding transition requires systemd_unit_file and nginx_config_path"
            )
        unit_text, unit_mode = self._read_binding_file(unit_path, "systemd_unit_file")
        nginx_text, nginx_mode = self._read_binding_file(nginx_path, "nginx_config")
        old_application_root = str(self.config.get("legacy_application_root") or "").strip()
        old_served_root = str(
            self.config.get("legacy_served_root") or
            self.config.get("legacy_reports_root") or ""
        ).strip()
        if old_application_root:
            old_application_root = _literal(old_application_root)
        if old_served_root:
            old_served_root = _literal(old_served_root)

        if self._contains_alias(nginx_text, self.current_reports) and \
                "WorkingDirectory={}".format(self.current_app) in unit_text:
            self.release_bindings_changed = False
            return {
                "status": "PASSED",
                "adapter": ADAPTER_NAME,
                "deployment_layout": "IMMUTABLE_CURRENT",
                "already_bound": True,
                "command": "validate existing CURRENT systemd/Nginx binding",
                "exit_code": 0,
            }
        if not old_application_root or not old_served_root:
            raise RuntimeError(
                "Flat vfoswind binding transition lacks legacy application/reports roots"
            )
        expected_working = "WorkingDirectory={}".format(old_application_root)
        if expected_working not in unit_text:
            raise RuntimeError(
                "systemd unit no longer matches the recorded Flat application root"
            )
        if not any(
                old_application_root in line and "enhance_coverage.py" in line
                for line in unit_text.splitlines()
                if line.strip().startswith("ExecStart=")
        ):
            raise RuntimeError(
                "systemd ExecStart no longer matches the recorded Flat application root"
            )
        old_alias = "alias {}/;".format(old_served_root.rstrip(os.sep))
        if nginx_text.count(old_alias) != 1:
            raise RuntimeError(
                "Nginx alias no longer matches the recorded Flat reports root"
            )

        self._previous_unit = unit_text.encode("utf-8")
        self._previous_unit_mode = unit_mode
        self._previous_nginx = nginx_text.encode("utf-8")
        self._previous_nginx_mode = nginx_mode
        updated_unit = []
        for line in unit_text.splitlines(True):
            if line.strip().startswith("WorkingDirectory="):
                line = line.replace(old_application_root, self.current_app, 1)
            elif line.strip().startswith("ExecStart="):
                line = line.replace(old_application_root, self.current_app)
            updated_unit.append(line)
        updated_nginx = nginx_text.replace(
            old_alias, "alias {}/;".format(self.current_reports.rstrip(os.sep)), 1
        )
        try:
            self._replace_atomic(unit_path, "".join(updated_unit), unit_mode)
            self._replace_atomic(nginx_path, updated_nginx, nginx_mode)
            rebound_unit = self._read_file(unit_path, "systemd_unit_file")
            rebound_nginx = self._read_file(nginx_path, "nginx_config")
            if "WorkingDirectory={}".format(self.current_app) not in rebound_unit or \
                    not any(
                        self.current_app in line and "enhance_coverage.py" in line
                        for line in rebound_unit.splitlines()
                        if line.strip().startswith("ExecStart=")
                    ) or not self._contains_alias(rebound_nginx, self.current_reports):
                raise RuntimeError("vfoswind production binding did not verify after atomic update")
        except Exception:
            self.restore_previous_release_bindings()
            raise
        self.release_bindings_changed = True
        return {
            "status": "PASSED",
            "adapter": ADAPTER_NAME,
            "deployment_layout": "FLAT_TO_IMMUTABLE_CURRENT",
            "already_bound": False,
            "systemd_unit_file": unit_path,
            "systemd_working_directory": self.current_app,
            "nginx_config_path": nginx_path,
            "nginx_alias": self.current_reports,
            "command": "atomic systemd/Nginx CURRENT binding transition",
            "exit_code": 0,
        }

    def restore_previous_release_bindings(self):
        """Restore the exact systemd/Nginx bytes captured before adoption."""
        if self._previous_unit is None and self._previous_nginx is None:
            return {"status": "PASSED", "skipped": True,
                    "reason": "no_release_binding_transition"}
        if self._previous_unit is not None:
            self._replace_atomic(
                self._unit_file_path(), self._previous_unit.decode("utf-8"),
                self._previous_unit_mode,
            )
        if self._previous_nginx is not None:
            self._replace_atomic(
                self._nginx_file_path(), self._previous_nginx.decode("utf-8"),
                self._previous_nginx_mode,
            )
        self.release_bindings_changed = False
        return {
            "status": "PASSED",
            "adapter": ADAPTER_NAME,
            "restored_previous_release_bindings": True,
            "command": "atomic systemd/Nginx binding rollback",
            "exit_code": 0,
        }

    def _write_environment(self, content, mode=None):
        directory = os.path.dirname(self.environment_file) or "."
        if not os.path.isdir(directory):
            raise RuntimeError("production EnvironmentFile directory is missing")
        descriptor, temporary = tempfile.mkstemp(
            prefix=".coverage-runtime-", dir=directory
        )
        try:
            if isinstance(content, bytes):
                stream = os.fdopen(descriptor, "wb")
            else:
                stream = os.fdopen(descriptor, "w", encoding="utf-8")
            with stream:
                stream.write(content)
                stream.flush()
                try:
                    os.fsync(stream.fileno())
                except OSError:
                    pass
            if mode is not None:
                os.chmod(temporary, mode)
            os.replace(temporary, self.environment_file)
        finally:
            try:
                os.remove(temporary)
            except OSError:
                pass

    def bind_candidate_database(self, target):
        """Persist only the Candidate runtime DB binding after CURRENT switch."""
        target_mysql = _runtime_mysql_config(target)
        with open(self.environment_file, "rb") as stream:
            previous = stream.read()
        self._previous_environment = previous
        self._previous_environment_mode = os.stat(self.environment_file).st_mode & 0o777
        text = previous.decode("utf-8")
        rendered = "COVERAGE_MYSQL_JSON={}".format(
            _quote_environment_value(json.dumps(
                target_mysql, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ))
        )
        lines = text.splitlines(True)
        replaced = False
        updated = []
        for line in lines:
            if line.lstrip().startswith("COVERAGE_MYSQL_JSON="):
                updated.append(rendered + ("\n" if line.endswith("\n") else ""))
                replaced = True
            else:
                updated.append(line)
        if not replaced:
            if updated and not updated[-1].endswith("\n"):
                updated[-1] += "\n"
            updated.append(rendered + "\n")
        self._write_environment("".join(updated), self._previous_environment_mode)
        self.runtime_bound = True
        return {
            "status": "PASSED",
            "adapter": ADAPTER_NAME,
            "runtime_environment_file": self.environment_file,
            "target_database": target_mysql.get("database"),
            "runtime_binding": "COVERAGE_MYSQL_JSON",
            "credentials_written_to_evidence": False,
            "command": "atomic EnvironmentFile database binding",
            "exit_code": 0,
        }

    def daemon_reload(self):
        result = self._run("daemon_reload")
        if result["status"] != "PASSED":
            raise RuntimeError("systemd daemon-reload failed")
        return result

    def stop_service(self):
        result = self._run("service_stop")
        if result["status"] != "PASSED":
            raise RuntimeError("vfoswind systemd service stop failed")
        return result

    def restart_service(self):
        result = self._run("service_restart")
        if result["status"] != "PASSED":
            raise RuntimeError("vfoswind systemd service restart failed")
        return result

    def reload_nginx(self):
        test = self._run("nginx_test")
        if test["status"] != "PASSED":
            raise RuntimeError("nginx -t failed")
        reload_result = self._run("nginx_reload")
        if reload_result["status"] != "PASSED":
            raise RuntimeError("nginx reload failed")
        return {"status": "PASSED", "nginx_test": test, "nginx_reload": reload_result}

    def restore_previous_database_binding(self):
        """Restore the exact pre-cutover EnvironmentFile during rollback."""
        if self._previous_environment is None:
            return {"status": "PASSED", "skipped": True, "reason": "no_candidate_binding"}
        self._write_environment(
            self._previous_environment, self._previous_environment_mode
        )
        self.runtime_bound = False
        return {
            "status": "PASSED",
            "adapter": ADAPTER_NAME,
            "runtime_environment_file": self.environment_file,
            "restored_previous_binding": True,
            "credentials_written_to_evidence": False,
            "command": "atomic EnvironmentFile rollback",
            "exit_code": 0,
        }
