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
import re
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
        self._previous_unit_was_absent = False
        self._previous_nginx = None
        self._previous_nginx_mode = None
        self.release_bindings_changed = False
        self.validation_binding_changed = False
        self.runtime_bound = False
        self.bootstrap_required = False
        self.bootstrap_plan = {}
        self._bootstrap_validation_installed = False
        self._bootstrap_validation_previous = {}
        self._bootstrap_runtime_environment_created = False
        self._bootstrap_previous_environment = None
        self._bootstrap_previous_environment_mode = None
        self._bootstrap_runtime_environment_touched = False

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
            with open(path, "r", encoding="utf-8", newline="") as stream:
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

    def _bootstrap_config(self):
        value = self.config.get("bootstrap") or {}
        if not isinstance(value, dict):
            raise RuntimeError("production integration bootstrap must be an object")
        return value

    def _legacy_unit_path(self):
        bootstrap = self._bootstrap_config()
        configured = bootstrap.get("legacy_systemd_unit_file") or \
            self.config.get("legacy_systemd_unit_file") or \
            self._unit_file_path()
        return _literal(configured) if configured else ""

    def _legacy_nginx_path(self):
        bootstrap = self._bootstrap_config()
        configured = bootstrap.get("legacy_nginx_config_path") or \
            self.config.get("legacy_nginx_config_path") or \
            self._nginx_file_path()
        return _literal(configured) if configured else ""

    def _legacy_unit_name(self):
        bootstrap = self._bootstrap_config()
        return str(
            bootstrap.get("legacy_systemd_unit") or
            self.config.get("legacy_systemd_unit") or
            self.config.get("systemd_unit") or ""
        ).strip()

    def _legacy_unit_definition(self):
        path = self._legacy_unit_path()
        if path and os.path.isfile(path) and not os.path.islink(path):
            return self._read_file(path, "legacy_systemd_unit_file")
        unit = self._legacy_unit_name()
        if not unit:
            raise RuntimeError("legacy systemd unit identity is required")
        result = self.command_runner(["systemctl", "cat", unit])
        if result.returncode != 0:
            raise RuntimeError(
                "legacy systemd unit definition probe failed: {}".format(
                    result.stderr.decode("utf-8", errors="replace")
                )
            )
        return result.stdout.decode("utf-8", errors="replace")

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

    @staticmethod
    def _validate_legacy_runtime_unit(text, application_root):
        """Validate the observed Flat service without requiring new env files."""
        application_root = _literal(application_root)
        if "WorkingDirectory={}".format(application_root) not in text:
            raise RuntimeError(
                "legacy systemd WorkingDirectory is not bound to the recorded Flat application root"
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
                "legacy systemd ExecStart is not bound to the recorded Flat application root"
            )

    @staticmethod
    def _sha256_bytes(value):
        import hashlib
        return hashlib.sha256(value).hexdigest()

    @staticmethod
    def _replace_unit_application_and_environment(
            text, old_application_root, new_application_root, environment_file):
        old_application_root = _literal(old_application_root)
        new_application_root = _literal(new_application_root)
        lines = []
        environment_replaced = False
        service_section_seen = False
        for line in str(text).splitlines(True):
            stripped = line.strip()
            if stripped == "[Service]":
                service_section_seen = True
            if stripped.startswith("WorkingDirectory="):
                line = "WorkingDirectory={}\n".format(new_application_root)
            elif stripped.startswith("ExecStart="):
                line = line.replace(old_application_root, new_application_root)
            elif stripped.startswith("EnvironmentFile="):
                line = "EnvironmentFile={}\n".format(environment_file)
                environment_replaced = True
            lines.append(line)
            if service_section_seen and not environment_replaced and stripped == "[Service]":
                lines.append("EnvironmentFile={}\n".format(environment_file))
                environment_replaced = True
        if not service_section_seen:
            raise RuntimeError("legacy systemd unit has no [Service] section")
        if not environment_replaced:
            lines.append("EnvironmentFile={}\n".format(environment_file))
        return "".join(lines)

    def _bootstrap_static_probe(self, field, staged_path):
        bootstrap = self._bootstrap_config()
        configured = bootstrap.get(field)
        if not configured:
            raise RuntimeError(
                "production bootstrap command '{}' is required".format(field)
            )
        argv = _argv(configured, field)
        if "{path}" in argv:
            argv = [staged_path if item == "{path}" else item for item in argv]
        elif bootstrap.get("{}_uses_staged_path".format(field), True):
            argv.append(staged_path)
        result = self.command_runner(argv)
        evidence = {
            "name": field,
            "command": argv,
            "status": "PASSED" if result.returncode == 0 else "FAILED",
            "exit_code": result.returncode,
            "stdout": result.stdout.decode("utf-8", errors="replace")[-2000:],
            "stderr": result.stderr.decode("utf-8", errors="replace")[-2000:],
        }
        if result.returncode != 0:
            raise RuntimeError(
                "production bootstrap {} failed".format(field)
            )
        return evidence

    @staticmethod
    def _render_nginx_probe_main(managed_config_path, managed_config, stage):
        """Render a disposable nginx.conf that includes the managed bytes.

        The production file is normally a server or location snippet rather
        than a complete nginx main configuration.  Testing that snippet with
        ``nginx -t`` alone either tests the live configuration or produces a
        misleading parse context.  This wrapper supplies the real nginx main
        and http/server contexts while keeping every file under a disposable
        staging directory.
        """
        def quote_path(value):
            return str(value).replace("\\", "\\\\").replace('"', '\\"')

        include_line = 'include "{}";'.format(quote_path(managed_config_path))
        if re.search(r"(?m)^[ \t]*server[ \t]*\{", managed_config):
            http_body = include_line
        else:
            http_body = (
                "server {{\n"
                "    listen 127.0.0.1:19528;\n"
                "    {}\n"
                "}}\n"
            ).format(include_line)
        return (
            'pid "{}";\n'
            'error_log "{}" crit;\n'
            "events {{ worker_connections 16; }}\n"
            "http {{\n"
            "{}"
            "}}\n"
        ).format(
            quote_path(os.path.join(stage, "nginx.pid")),
            quote_path(os.path.join(stage, "nginx-error.log")),
            http_body,
        )

    def _bootstrap_nginx_static_probe(self, managed_config, stage):
        """Run nginx -t against the exact managed config being published.

        ``nginx_test_uses_staged_path=false`` used to make this gate execute
        against the live host configuration.  Bootstrap must never accept that
        as evidence for a new binding, so the command is always pointed at a
        temporary main config which includes the staged managed bytes.
        """
        bootstrap = self._bootstrap_config()
        configured = bootstrap.get("nginx_test")
        if not configured:
            raise RuntimeError(
                "production bootstrap command 'nginx_test' is required"
            )
        managed_path = os.path.join(stage, "managed.conf")
        main_path = os.path.join(stage, "nginx.conf")
        with open(managed_path, "w", encoding="utf-8", newline="") as stream:
            stream.write(str(managed_config))
        main_config = self._render_nginx_probe_main(
            managed_path, str(managed_config), stage
        )
        with open(main_path, "w", encoding="utf-8", newline="") as stream:
            stream.write(main_config)

        argv = _argv(configured, "nginx_test")
        if "{path}" in argv:
            argv = [main_path if item == "{path}" else item for item in argv]
        elif "-c" in argv:
            config_index = argv.index("-c") + 1
            if config_index >= len(argv):
                raise ValueError("production bootstrap nginx_test -c has no path")
            argv[config_index] = main_path
        else:
            argv.extend(["-c", main_path])
        result = self.command_runner(argv)
        evidence = {
            "name": "nginx_test",
            "command": argv,
            "status": "PASSED" if result.returncode == 0 else "FAILED",
            "exit_code": result.returncode,
            "stdout": result.stdout.decode("utf-8", errors="replace")[-2000:],
            "stderr": result.stderr.decode("utf-8", errors="replace")[-2000:],
            "probe_mode": "temporary_main_include",
            "staged_config_sha256": self._sha256_bytes(
                str(managed_config).encode("utf-8")
            ),
            "staged_main_config_sha256": self._sha256_bytes(
                main_config.encode("utf-8")
            ),
        }
        if result.returncode != 0:
            raise RuntimeError("production bootstrap nginx_test failed")
        return evidence

    def _build_flat_bootstrap_plan(
            self, unit_text, nginx_text, legacy_application_root,
            legacy_served_root, runtime_mysql, candidate_application_root,
            candidate_ports):
        unit_path = self._unit_file_path()
        nginx_path = self._nginx_file_path()
        legacy_unit_path = self._legacy_unit_path()
        legacy_nginx_path = self._legacy_nginx_path()
        if not unit_path or not nginx_path or not legacy_unit_path or not legacy_nginx_path:
            raise RuntimeError("Flat bootstrap requires explicit systemd/Nginx file paths")
        if unit_path != legacy_unit_path or nginx_path != legacy_nginx_path:
            raise RuntimeError(
                "Flat bootstrap requires stable systemd and Nginx binding paths"
            )
        if not candidate_application_root:
            raise RuntimeError("Flat bootstrap requires the immutable Candidate application root")
        ports = [int(port) for port in (candidate_ports or [])]
        if not ports:
            raise RuntimeError("Flat bootstrap requires an isolated Candidate port")
        runtime_mysql = _runtime_mysql_config(runtime_mysql)
        managed_unit = self._replace_unit_application_and_environment(
            unit_text, legacy_application_root, self.current_app,
            self.environment_file,
        )
        managed_nginx = nginx_text.replace(
            "alias {}/;".format(legacy_served_root.rstrip(os.sep)),
            "alias {}/;".format(self.current_reports.rstrip(os.sep)),
            1,
        )
        if managed_nginx == nginx_text or not self._contains_alias(
                managed_nginx, self.current_reports):
            raise RuntimeError("Flat bootstrap could not render the managed Nginx CURRENT alias")
        # The bootstrap plan is a binding template, not a serving credential
        # handoff.  Keep source secrets out of temporary validation files and
        # let bind_candidate_database write the target credentials only after
        # CURRENT has been switched.
        bootstrap_runtime_mysql = dict(runtime_mysql)
        bootstrap_runtime_mysql.pop("password", None)
        runtime_environment = self._render_mysql_environment(
            "", bootstrap_runtime_mysql
        )
        validation_config = {
            "server": {"host": "127.0.0.1", "port": ports[0]},
            "runtime_mode": "vnext",
            "environment": "candidate",
        }
        validation_config_text = json.dumps(
            validation_config, ensure_ascii=False, sort_keys=True, indent=2
        ) + "\n"
        validation_unit = (
            "[Unit]\n"
            "Description=onesensor coverage isolated Candidate validation\n"
            "[Service]\n"
            "Type=simple\n"
            "WorkingDirectory={application}\n"
            "ExecStart={python} {application}/enhance_coverage.py server\n"
            "EnvironmentFile={environment}\n"
        ).format(
            application=_literal(self.config.get("validation_application_root") or
                                 candidate_application_root),
            python=str(self._bootstrap_config().get(
                "python", "/usr/bin/python3"
            )),
            environment=self.validation_environment_file,
        )
        validation_environment = (
            "COVERAGE_CONFIG_PATH={}\n".format(self.validation_config_path) +
            "COVERAGE_MYSQL_JSON={}\n".format(
                _quote_environment_value(json.dumps(
                    bootstrap_runtime_mysql, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ))
            )
        )
        with tempfile.TemporaryDirectory(prefix="coverage-vfoswind-bootstrap-") as stage:
            unit_stage = os.path.join(stage, "managed.service")
            nginx_stage = os.path.join(stage, "managed.conf")
            # Round-trip the exact legacy bytes through disposable files.  The
            # managed rendering is checked separately by the read-only static
            # probes in _preflight_flat_bootstrap; keeping these stages
            # separate prevents a test fixture from accidentally verifying the
            # original bytes after overwriting the managed rendering.
            with open(unit_stage, "w", encoding="utf-8", newline="") as stream:
                stream.write(unit_text)
            with open(nginx_stage, "w", encoding="utf-8", newline="") as stream:
                stream.write(nginx_text)
            with open(unit_stage, "r", encoding="utf-8", newline="") as stream:
                restored_unit = stream.read()
            with open(nginx_stage, "r", encoding="utf-8", newline="") as stream:
                restored_nginx = stream.read()
            rollback_bytes_verified = (
                restored_unit == unit_text and restored_nginx == nginx_text
            )
        if not rollback_bytes_verified:
            raise RuntimeError("Flat bootstrap rollback byte verification failed")
        plan = {
            "status": "PASSED",
            "bootstrap_ready": True,
            "bootstrap_required": True,
            "legacy_systemd_unit": self._legacy_unit_name(),
            "legacy_systemd_unit_file": legacy_unit_path,
            "legacy_nginx_config_path": legacy_nginx_path,
            "legacy_application_root": _literal(legacy_application_root),
            "legacy_served_root": _literal(legacy_served_root),
            "managed_systemd_unit_file": unit_path,
            "managed_nginx_config_path": nginx_path,
            "legacy_systemd_unit_definition": unit_text,
            "managed_systemd_unit": managed_unit,
            "managed_nginx": managed_nginx,
            "runtime_environment": runtime_environment,
            "validation_unit": validation_unit,
            "validation_environment": validation_environment,
            "validation_config": validation_config_text,
            "validation_application_root": _literal(
                self.config.get("validation_application_root") or
                candidate_application_root
            ),
            "validation_ports": ports,
            "old_unit_sha256": self._sha256_bytes(unit_text.encode("utf-8")),
            "old_nginx_sha256": self._sha256_bytes(nginx_text.encode("utf-8")),
            "managed_unit_sha256": self._sha256_bytes(managed_unit.encode("utf-8")),
            "managed_nginx_sha256": self._sha256_bytes(managed_nginx.encode("utf-8")),
            "rollback_bytes_verified": rollback_bytes_verified,
        }
        return plan

    def _preflight_flat_bootstrap(
            self, expected_database, runtime_mysql, candidate_application_root,
            candidate_ports):
        """Plan the one-time Flat-to-managed transition without writing host files."""
        legacy_application_root = str(
            self.config.get("legacy_application_root") or ""
        ).strip()
        legacy_served_root = str(
            self.config.get("legacy_served_root") or
            self.config.get("legacy_reports_root") or ""
        ).strip()
        if not legacy_application_root or not legacy_served_root:
            raise RuntimeError(
                "Flat vfoswind bootstrap requires legacy application and reports roots"
            )
        legacy_application_root = _literal(legacy_application_root)
        legacy_served_root = _literal(legacy_served_root)
        if not os.path.isdir(legacy_served_root) or os.path.islink(legacy_served_root):
            raise RuntimeError("legacy vfoswind reports root is not a real directory")

        unit_text = self._legacy_unit_definition()
        self._validate_legacy_runtime_unit(unit_text, legacy_application_root)
        nginx_path = self._legacy_nginx_path()
        nginx_text = self._read_file(nginx_path, "legacy_nginx_config")
        if not self._contains_alias(nginx_text, legacy_served_root):
            raise RuntimeError("legacy Nginx alias is not bound to the recorded Flat reports root")
        expected_proxy = str(self.config.get("nginx_proxy_pass") or "").strip()
        if expected_proxy and expected_proxy not in nginx_text:
            raise RuntimeError("legacy Nginx proxy_pass does not match the production binding")

        runtime = _runtime_mysql_config(runtime_mysql)
        if expected_database and runtime.get("database", "").lower() != str(
                expected_database
        ).lower():
            raise RuntimeError("Flat bootstrap runtime database does not match source identity")
        validation_application = str(
            candidate_application_root or
            self.config.get("validation_application_root") or ""
        ).strip()
        if not validation_application:
            raise RuntimeError("Flat bootstrap Candidate application root is required")
        validation_application = _literal(validation_application)
        application_evidence = validate_production_application_root(
            validation_application
        )
        plan = self._build_flat_bootstrap_plan(
            unit_text, nginx_text, legacy_application_root,
            legacy_served_root, runtime, validation_application,
            candidate_ports,
        )
        with tempfile.TemporaryDirectory(prefix="coverage-vfoswind-bootstrap-check-") as stage:
            unit_stage = os.path.join(stage, "managed.service")
            with open(unit_stage, "w", encoding="utf-8") as stream:
                stream.write(plan["managed_systemd_unit"])
            systemd_check = self._bootstrap_static_probe(
                "systemd_analyze", unit_stage
            )
            nginx_check = self._bootstrap_nginx_static_probe(
                plan["managed_nginx"], stage
            )
        plan["systemd_analyze"] = systemd_check
        plan["nginx_test"] = nginx_check
        plan["runtime_database"] = runtime.get("database", "")
        plan["runtime_application_user"] = runtime.get("user", "")
        plan["application"] = application_evidence
        plan["read_only"] = True
        plan["command"] = "read legacy vfoswind bindings + render managed bootstrap + static checks"
        plan["exit_code"] = 0
        self.bootstrap_required = True
        self.bootstrap_plan = plan
        return {
            "status": "PASSED",
            "adapter": ADAPTER_NAME,
            "bootstrap_ready": True,
            "bootstrap_required": True,
            "deployment_layout": "FLAT",
            "transition_required": True,
            "legacy_systemd_unit": plan["legacy_systemd_unit"],
            "legacy_systemd_unit_file": plan["legacy_systemd_unit_file"],
            "legacy_nginx_config_path": plan["legacy_nginx_config_path"],
            "legacy_application_root": plan["legacy_application_root"],
            "legacy_served_root": plan["legacy_served_root"],
            "managed_systemd_unit_file": plan["managed_systemd_unit_file"],
            "managed_nginx_config_path": plan["managed_nginx_config_path"],
            "runtime_database": plan["runtime_database"],
            "runtime_application_user": plan["runtime_application_user"],
            "candidate_application_root": validation_application,
            "candidate_artifact_sha256": application_evidence.get("application_sha256", ""),
            "validation_ports": plan["validation_ports"],
            "old_unit_sha256": plan["old_unit_sha256"],
            "old_nginx_sha256": plan["old_nginx_sha256"],
            "managed_unit_sha256": plan["managed_unit_sha256"],
            "managed_nginx_sha256": plan["managed_nginx_sha256"],
            "rollback_bytes_verified": plan["rollback_bytes_verified"],
            "systemd_analyze": systemd_check,
            "nginx_test": nginx_check,
            "read_only": True,
            "command": plan["command"],
            "exit_code": 0,
        }

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
                  candidate_ports=None, validation_commands=None,
                  runtime_mysql=None):
        """Read and validate systemd, Nginx, and persistent DB bindings."""
        if str(self.config.get("adapter") or ADAPTER_NAME).strip() != ADAPTER_NAME:
            raise RuntimeError("unsupported production lifecycle adapter")
        self._validate_commands(validation_commands=validation_commands)
        current_path = os.path.join(self.publish_root, "CURRENT")
        if os.path.lexists(current_path) and not os.path.islink(current_path):
            raise RuntimeError("vfoswind CURRENT exists but is not an immutable symlink")
        current_exists = os.path.islink(current_path)
        if not current_exists:
            return self._preflight_flat_bootstrap(
                expected_database=expected_database,
                runtime_mysql=runtime_mysql,
                candidate_application_root=candidate_application_root,
                candidate_ports=candidate_ports,
            )
        unit_text = self._unit_definition()
        expected_environment = "EnvironmentFile={}".format(self.environment_file)
        if expected_environment not in unit_text and \
                ("EnvironmentFile=-{}".format(self.environment_file) not in unit_text):
            raise RuntimeError(
                "systemd unit does not persist the configured EnvironmentFile"
            )
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

    @staticmethod
    def _optional_file_state(path, field):
        if not os.path.lexists(path):
            return None, None
        if os.path.islink(path) or not os.path.isfile(path):
            raise RuntimeError(
                "production integration {} must be a regular file when present: {}".format(
                    field, path
                )
            )
        with open(path, "rb") as stream:
            content = stream.read()
        return content, os.stat(path).st_mode & 0o777

    def _install_bootstrap_validation_bindings(self):
        """Install only the isolated validation files from a preflight plan."""
        if not self.bootstrap_required:
            return {"status": "PASSED", "skipped": True, "reason": "managed_layout"}
        plan = dict(self.bootstrap_plan or {})
        paths = {
            "unit": self._validation_unit_file_path(),
            "environment": self.validation_environment_file,
            "config": self.validation_config_path,
        }
        states = {}
        present = []
        for key, path in paths.items():
            content, mode = self._optional_file_state(path, "validation_{}".format(key))
            states[key] = (content, mode)
            present.append(content is not None)
        if any(present):
            if not all(present):
                raise RuntimeError(
                    "Flat bootstrap validation files are partially provisioned"
                )
            self._bootstrap_validation_previous = {}
            return {
                "status": "PASSED",
                "skipped": True,
                "reason": "validation_bootstrap_already_provisioned",
            }
        plan_keys = {
            "unit": "validation_unit",
            "environment": "validation_environment",
            "config": "validation_config",
        }
        for key, plan_key in plan_keys.items():
            if plan_key not in plan:
                raise RuntimeError(
                    "Flat bootstrap validation plan is incomplete: {}".format(
                        plan_key
                    )
                )
        self._bootstrap_validation_previous = states
        try:
            self._replace_atomic(paths["unit"], plan[plan_keys["unit"]], 0o644)
            self._replace_atomic(
                paths["environment"], plan[plan_keys["environment"]], 0o600
            )
            self._replace_atomic(
                paths["config"], plan[plan_keys["config"]], 0o644
            )
        except Exception:
            self._restore_bootstrap_validation_bindings()
            raise
        self._bootstrap_validation_installed = True
        return {
            "status": "PASSED",
            "installed": True,
            "validation_systemd_unit_file": paths["unit"],
            "validation_runtime_environment_file": paths["environment"],
            "validation_config_path": paths["config"],
            "command": "atomic Flat bootstrap validation unit/EnvironmentFile/config provision",
            "exit_code": 0,
        }

    def _restore_bootstrap_validation_bindings(self):
        if not self._bootstrap_validation_previous:
            return {"status": "PASSED", "skipped": True,
                    "reason": "no_bootstrap_validation_files"}
        paths = {
            "unit": self._validation_unit_file_path(),
            "environment": self.validation_environment_file,
            "config": self.validation_config_path,
        }
        for key, path in paths.items():
            previous, mode = self._bootstrap_validation_previous.get(
                key, (None, None)
            )
            if previous is None:
                if os.path.lexists(path):
                    if os.path.islink(path) or not os.path.isfile(path):
                        raise RuntimeError(
                            "cannot remove unexpected bootstrap validation file: {}".format(
                                path
                            )
                        )
                    os.remove(path)
            else:
                self._replace_atomic(path, previous, mode)
        self._bootstrap_validation_previous = {}
        self._bootstrap_validation_installed = False
        return {
            "status": "PASSED",
            "restored": True,
            "command": "restore exact pre-bootstrap validation files",
            "exit_code": 0,
        }

    def _install_bootstrap_runtime_environment(self):
        """Create the managed serving EnvironmentFile only at cutover."""
        if not self.bootstrap_required:
            return {"status": "PASSED", "skipped": True, "reason": "managed_layout"}
        plan = dict(self.bootstrap_plan or {})
        content, mode = self._optional_file_state(
            self.environment_file, "runtime_environment_file"
        )
        self._bootstrap_previous_environment = content
        self._bootstrap_previous_environment_mode = mode
        self._bootstrap_runtime_environment_touched = True
        if content is not None:
            _observed_text, observed_runtime = self._read_runtime_mysql_binding(
                self.environment_file, "runtime_environment_file"
            )
            if observed_runtime.get("database") != plan.get("runtime_database"):
                raise RuntimeError(
                    "existing serving EnvironmentFile database does not match Flat source identity"
                )
            return {
                "status": "PASSED",
                "created": False,
                "runtime_database": observed_runtime.get("database"),
                "command": "validate existing serving EnvironmentFile during bootstrap",
                "exit_code": 0,
            }
        if not plan.get("runtime_environment"):
            raise RuntimeError("Flat bootstrap runtime EnvironmentFile plan is missing")
        self._replace_atomic(
            self.environment_file, plan["runtime_environment"], 0o600
        )
        self._bootstrap_runtime_environment_created = True
        return {
            "status": "PASSED",
            "created": True,
            "runtime_environment_file": self.environment_file,
            "runtime_database": plan.get("runtime_database"),
            "command": "atomic serving EnvironmentFile bootstrap provision",
            "exit_code": 0,
        }

    def _restore_bootstrap_runtime_environment(self):
        if not self._bootstrap_runtime_environment_touched:
            return {"status": "PASSED", "skipped": True,
                    "reason": "no_bootstrap_runtime_environment"}
        if self._bootstrap_previous_environment is None:
            if os.path.lexists(self.environment_file):
                if os.path.islink(self.environment_file) or not os.path.isfile(
                        self.environment_file
                ):
                    raise RuntimeError(
                        "cannot remove unexpected bootstrap EnvironmentFile"
                    )
                os.remove(self.environment_file)
        else:
            self._replace_atomic(
                self.environment_file,
                self._bootstrap_previous_environment,
                self._bootstrap_previous_environment_mode,
            )
        self._bootstrap_runtime_environment_created = False
        self._bootstrap_previous_environment = None
        self._bootstrap_previous_environment_mode = None
        self._bootstrap_runtime_environment_touched = False
        return {
            "status": "PASSED",
            "restored": True,
            "command": "restore exact pre-bootstrap serving EnvironmentFile",
            "exit_code": 0,
        }

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
        bootstrap_validation = None
        try:
            # The bootstrap installer may create the isolated validation files.
            # Keep every subsequent read/validation in this try block so a
            # malformed generated binding cannot leak those files.
            bootstrap_validation = self._install_bootstrap_validation_bindings()
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
            "bootstrap_validation": bootstrap_validation,
            "command": "atomic validation systemd/Candidate EnvironmentFile binding",
            "credentials_written_to_evidence": False,
            "exit_code": 0,
        }

    def restore_validation_candidate_binding(self):
        """Restore the exact pre-validation unit and database binding."""
        if self._previous_validation_unit is None and \
                self._previous_validation_environment is None:
            bootstrap_restore = self._restore_bootstrap_validation_bindings()
            return {
                "status": "PASSED", "skipped": True,
                "reason": "no_validation_candidate_binding",
                "bootstrap_validation_restore": bootstrap_restore,
            }
        if self._previous_validation_unit is not None:
            self._replace_atomic(
                self._validation_unit_file_path(),
                self._previous_validation_unit,
                self._previous_validation_unit_mode,
            )
        if self._previous_validation_environment is not None:
            self._replace_atomic(
                self.validation_environment_file,
                self._previous_validation_environment,
                self._previous_validation_environment_mode,
            )
        bootstrap_restore = self._restore_bootstrap_validation_bindings()
        self.validation_binding_changed = False
        return {
            "status": "PASSED",
            "adapter": ADAPTER_NAME,
            "restored_previous_validation_binding": True,
            "bootstrap_validation_restore": bootstrap_restore,
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
        unit_was_absent = False
        if self.bootstrap_required and not os.path.lexists(unit_path):
            legacy_definition = (self.bootstrap_plan or {}).get(
                "legacy_systemd_unit_definition"
            )
            if not legacy_definition:
                raise RuntimeError(
                    "Flat bootstrap plan lacks the legacy systemd unit definition"
                )
            # systemctl cat may have resolved a vendor unit outside the
            # configured persistent override path.  Treat the path as a new
            # managed override and remember that rollback must remove it.
            unit_text, unit_mode = legacy_definition, 0o644
            unit_was_absent = True
        else:
            unit_text, unit_mode = self._read_binding_file(
                unit_path, "systemd_unit_file"
            )
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

        bootstrap_runtime = None
        if self.bootstrap_required:
            plan = dict(self.bootstrap_plan or {})
            if plan.get("managed_systemd_unit_file") != unit_path or \
                    plan.get("managed_nginx_config_path") != nginx_path:
                raise RuntimeError("Flat bootstrap plan binding paths changed before cutover")
            if self._sha256_bytes(unit_text.encode("utf-8")) != plan.get(
                    "old_unit_sha256"
            ) or self._sha256_bytes(nginx_text.encode("utf-8")) != plan.get(
                    "old_nginx_sha256"
            ):
                raise RuntimeError(
                    "Flat vfoswind binding changed after bootstrap preflight"
                )
            if plan.get("rollback_bytes_verified") is not True:
                raise RuntimeError("Flat bootstrap rollback byte gate is not PASSED")
            self._previous_unit = unit_text.encode("utf-8")
            self._previous_unit_mode = unit_mode
            self._previous_unit_was_absent = unit_was_absent
            self._previous_nginx = nginx_text.encode("utf-8")
            self._previous_nginx_mode = nginx_mode
            try:
                bootstrap_runtime = self._install_bootstrap_runtime_environment()
                updated_unit = plan.get("managed_systemd_unit")
                updated_nginx = plan.get("managed_nginx")
                if not updated_unit or not updated_nginx:
                    raise RuntimeError("Flat bootstrap managed binding plan is incomplete")
            except Exception:
                self._restore_bootstrap_runtime_environment()
                raise
        else:
            updated_unit = None
            updated_nginx = None

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

        if not self.bootstrap_required:
            self._previous_unit = unit_text.encode("utf-8")
            self._previous_unit_mode = unit_mode
            self._previous_unit_was_absent = False
            self._previous_nginx = nginx_text.encode("utf-8")
            self._previous_nginx_mode = nginx_mode
            updated_unit_lines = []
            for line in unit_text.splitlines(True):
                if line.strip().startswith("WorkingDirectory="):
                    line = line.replace(old_application_root, self.current_app, 1)
                elif line.strip().startswith("ExecStart="):
                    line = line.replace(old_application_root, self.current_app)
                updated_unit_lines.append(line)
            updated_unit = "".join(updated_unit_lines)
            updated_nginx = nginx_text.replace(
                old_alias, "alias {}/;".format(self.current_reports.rstrip(os.sep)), 1
            )
        try:
            self._replace_atomic(unit_path, updated_unit, unit_mode)
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
            "bootstrap_runtime": bootstrap_runtime,
            "command": "atomic systemd/Nginx CURRENT binding transition",
            "exit_code": 0,
        }

    def restore_previous_release_bindings(self):
        """Restore the exact systemd/Nginx bytes captured before adoption."""
        if self._previous_unit is None and self._previous_nginx is None:
            runtime_restore = {
                "status": "PASSED", "skipped": True,
                "reason": "deferred_to_database_binding_rollback",
            } if self.runtime_bound else self._restore_bootstrap_runtime_environment()
            return {"status": "PASSED", "skipped": True,
                    "reason": "no_release_binding_transition",
                    "runtime_environment_restore": runtime_restore}
        if self._previous_unit is not None:
            unit_path = self._unit_file_path()
            if self._previous_unit_was_absent:
                if os.path.lexists(unit_path):
                    if os.path.islink(unit_path) or not os.path.isfile(unit_path):
                        raise RuntimeError(
                            "cannot remove unexpected bootstrapped systemd unit file"
                        )
                    os.remove(unit_path)
            else:
                self._replace_atomic(
                    unit_path, self._previous_unit, self._previous_unit_mode,
                )
        if self._previous_nginx is not None:
            self._replace_atomic(
                self._nginx_file_path(), self._previous_nginx,
                self._previous_nginx_mode,
            )
        runtime_restore = {
            "status": "PASSED", "skipped": True,
            "reason": "deferred_to_database_binding_rollback",
        } if self.runtime_bound else self._restore_bootstrap_runtime_environment()
        self.release_bindings_changed = False
        self._previous_unit_was_absent = False
        return {
            "status": "PASSED",
            "adapter": ADAPTER_NAME,
            "restored_previous_release_bindings": True,
            "runtime_environment_restore": runtime_restore,
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
        bootstrap_restore = self._restore_bootstrap_runtime_environment()
        self.runtime_bound = False
        return {
            "status": "PASSED",
            "adapter": ADAPTER_NAME,
            "runtime_environment_file": self.environment_file,
            "restored_previous_binding": True,
            "bootstrap_runtime_environment_restore": bootstrap_restore,
            "credentials_written_to_evidence": False,
            "command": "atomic EnvironmentFile rollback",
            "exit_code": 0,
        }
