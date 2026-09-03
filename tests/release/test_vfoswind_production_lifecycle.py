import json
import os
import shutil
import tempfile
import unittest
from subprocess import CompletedProcess

from scripts.upgrade.vfoswind_production_lifecycle import (
    VfoswindProductionLifecycle, parse_environment_file,
)


class VfoswindProductionLifecycleTest(unittest.TestCase):
    def test_production_example_binds_observed_vfoswind_baseline(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "config", "coverage_config.production.vfoswind.example.json",
        )
        with open(path, "r", encoding="utf-8") as stream:
            config = json.load(stream)
        integration = config["upgrade"]["production_integration"]
        self.assertEqual(integration["systemd_unit"], "onesensor-api.service")
        self.assertEqual(
            integration["nginx_config_path"], "/etc/nginx/conf.d/coverage.conf"
        )
        self.assertEqual(
            integration["bootstrap"]["legacy_systemd_unit"],
            "onesensor-api.service",
        )
        self.assertEqual(
            integration["bootstrap"]["legacy_nginx_config_path"],
            "/etc/nginx/conf.d/coverage.conf",
        )
        self.assertEqual(
            integration["bootstrap"]["nginx_probe_mode"],
            "temporary_main_include",
        )
        self.assertEqual(
            integration["legacy_application_root"],
            "/home/zcyu/coverage/onesensor_code-coverage_tool",
        )
        self.assertTrue(
            config["upgrade"]["candidate_browser_url"].startswith("https://")
        )
        self.assertNotIn(
            "127.0.0.1", config["upgrade"]["candidate_browser_url"]
        )
        self.assertIn(
            "candidate_gateway", integration
        )
        self.assertEqual(
            integration["candidate_gateway"]["reports_root"],
            "/home/zcyu/coverage_published/VALIDATION_CURRENT/reports",
        )
        self.assertIn(
            "{commit_sha}",
            config["upgrade"]["ci_browser_fixture_evidence_path"],
        )

    def _fixture(self, flat=False):
        root = tempfile.TemporaryDirectory(prefix="vfoswind-lifecycle-")
        self.addCleanup(root.cleanup)
        publish = os.path.join(root.name, "published")
        if flat:
            current_app = os.path.join(root.name, "legacy-app")
            current_reports = os.path.join(root.name, "legacy-reports")
            os.makedirs(current_app)
            os.makedirs(current_reports)
        else:
            baseline = os.path.join(publish, "releases", "baseline")
            os.makedirs(os.path.join(baseline, "app"))
            os.makedirs(os.path.join(baseline, "reports"))
            os.symlink(os.path.join("releases", "baseline"),
                       os.path.join(publish, "CURRENT"))
            current_app = os.path.join(publish, "CURRENT", "app")
            current_reports = os.path.join(publish, "CURRENT", "reports")
        unit = os.path.join(root.name, "coverage.service")
        environment = os.path.join(root.name, "coverage-runtime.env")
        validation_unit = os.path.join(root.name, "coverage-validation.service")
        validation_environment = os.path.join(
            root.name, "coverage-validation.env"
        )
        validation_config = os.path.join(root.name, "coverage-validation.json")
        validation_application = os.path.join(root.name, "validation-app")
        nginx = os.path.join(root.name, "coverage-nginx.conf")
        application_directories = ("app", "web", "contracts")
        if not flat:
            application_directories += (os.path.join("scripts", "compat"),)
        for directory in application_directories:
            os.makedirs(os.path.join(current_app, directory))
        application_files = [
                ("enhance_coverage.py", "#!/usr/bin/env python3\n"),
                (os.path.join("app", "__init__.py"), ""),
                (os.path.join("app", "bootstrap.py"), ""),
        ]
        if not flat:
            application_files.append(
                (os.path.join("scripts", "compat", "git"), "#!/bin/sh\n")
            )
        for relative, contents in application_files:
            path = os.path.join(current_app, relative)
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(contents)
        if not flat:
            os.chmod(os.path.join(current_app, "scripts", "compat", "git"), 0o755)
        for directory in ("app", "web", "contracts", os.path.join("scripts", "compat")):
            os.makedirs(os.path.join(validation_application, directory))
        for relative, contents in (
                ("enhance_coverage.py", "#!/usr/bin/env python3\n"),
                (os.path.join("app", "__init__.py"), ""),
                (os.path.join("app", "bootstrap.py"), ""),
                (os.path.join("scripts", "compat", "git"), "#!/bin/sh\n")):
            path = os.path.join(validation_application, relative)
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(contents)
        os.chmod(
            os.path.join(validation_application, "scripts", "compat", "git"),
            0o755,
        )
        with open(unit, "w", encoding="utf-8") as stream:
            stream.write(
                "[Service]\n"
                "WorkingDirectory={}\n"
                "ExecStart=/usr/bin/python3 {}/enhance_coverage.py server\n"
                "{}".format(
                    current_app, current_app,
                    "EnvironmentFile={}\n".format(environment) if not flat else "",
                )
            )
        with open(environment, "w", encoding="utf-8") as stream:
            stream.write(
                "# stable serving binding\n"
                "COVERAGE_MYSQL_JSON=\"{\\\"database\\\":\\\"coverage_vnext_old\\\",\\\"user\\\":\\\"coverage_user\\\"}\"\n"
            )
        with open(validation_unit, "w", encoding="utf-8") as stream:
            stream.write(
                "[Service]\n"
                "WorkingDirectory={}\n"
                "ExecStart=/usr/bin/python3 {}/enhance_coverage.py server\n"
                "EnvironmentFile={}\n".format(
                    validation_application, validation_application,
                    validation_environment,
                )
            )
        with open(validation_environment, "w", encoding="utf-8") as stream:
            stream.write(
                "COVERAGE_CONFIG_PATH={}\n"
                "COVERAGE_MYSQL_JSON=\"{{\\\"database\\\":\\\"coverage_vnext_candidate_old\\\",\\\"user\\\":\\\"coverage_user\\\"}}\"\n".format(
                    validation_config
                )
            )
        with open(validation_config, "w", encoding="utf-8") as stream:
            json.dump({
                "server": {"port": 19528},
                "auth": {
                    "mode": "reverse_proxy",
                    "user_header": "X-Remote-User",
                    "trusted_proxy_addresses": ["127.0.0.1"],
                },
            }, stream)
        with open(nginx, "w", encoding="utf-8") as stream:
            auth_lines = "" if flat else (
                "  auth_basic Coverage;\n"
                "  auth_basic_user_file /etc/nginx/.coverage_htpasswd;\n"
                "  proxy_set_header X-Remote-User $remote_user;\n"
            )
            stream.write(
                "location /coverage/ {{\n"
                "  alias {}/;\n"
                "  proxy_pass http://127.0.0.1:9528;\n"
                "{}"
                "}}\n".format(current_reports, auth_lines)
            )
        if flat:
            os.remove(environment)
            for path in (validation_unit, validation_environment, validation_config):
                os.remove(path)
        commands = {
            "daemon_reload": ["systemctl", "daemon-reload"],
            "service_stop": ["systemctl", "stop", "onesensor-api.service"],
            "service_restart": ["systemctl", "restart", "onesensor-api.service"],
            "nginx_test": ["nginx", "-t", "-c", nginx],
            "nginx_reload": ["systemctl", "reload", "nginx"],
        }
        config = {
            "adapter": "vfoswind",
            "systemd_unit": "onesensor-api.service",
            "systemd_unit_file": unit,
            "runtime_environment_file": environment,
            "validation_systemd_unit": "onesensor-coverage-validation.service",
            "validation_systemd_unit_file": validation_unit,
            "validation_runtime_environment_file": validation_environment,
            "validation_config_path": validation_config,
            "validation_application_root": validation_application,
           "nginx_config_path": nginx,
           "nginx_proxy_pass": "http://127.0.0.1:9528",
            "api_location": "/coverage/",
            "auth_bridge": {
                "mode": "basic_auth",
                "user_header": "X-Remote-User",
                "identity_expression": "$remote_user",
                "auth_source": "/etc/nginx/.coverage_htpasswd",
            },
            "legacy_application_root": current_app,
            "legacy_served_root": current_reports,
            "bootstrap": {
                "legacy_systemd_unit": "onesensor-api.service",
                "legacy_systemd_unit_file": unit,
                "legacy_nginx_config_path": nginx,
                "systemd_analyze": ["systemd-analyze", "verify"],
                "nginx_test": ["nginx", "-t"],
                "nginx_probe_mode": "temporary_main_include",
            },
            "commands": commands,
        }
        return root, publish, config, environment

    def test_preflight_binds_systemd_nginx_and_persistent_database(self):
        _root, publish, config, _environment = self._fixture()
        adapter = VfoswindProductionLifecycle(publish, config)
        evidence = adapter.preflight()
        self.assertEqual(evidence["status"], "PASSED")
        self.assertEqual(evidence["adapter"], "vfoswind")
        self.assertEqual(evidence["runtime_database"], "coverage_vnext_old")
        self.assertTrue(evidence["read_only"])

    def test_candidate_database_binding_is_atomic_and_restorable(self):
        _root, publish, config, environment = self._fixture()
        calls = []

        def runner(argv):
            calls.append(list(argv))
            return CompletedProcess(argv, 0, b"", b"")

        adapter = VfoswindProductionLifecycle(
            publish, config, command_runner=runner
        )
        with open(environment, "rb") as stream:
            before = stream.read()
        target = {
            "host": "db",
            "port": 3306,
            "user": "coverage_user",
            "password": "candidate-secret",
            "database": "coverage_vnext_candidate",
            "backup_restore_user": "root",
            "backup_restore_password": "admin-secret",
        }
        binding = adapter.bind_candidate_database(target)
        self.assertEqual(binding["status"], "PASSED")
        with open(environment, "r", encoding="utf-8") as stream:
            values = parse_environment_file(stream.read())
        runtime = json.loads(values["COVERAGE_MYSQL_JSON"])
        self.assertEqual(runtime["database"], target["database"])
        self.assertNotIn("backup_restore_user", runtime)
        self.assertNotIn("backup_restore_password", runtime)
        adapter.daemon_reload()
        adapter.restart_service()
        adapter.reload_nginx()
        self.assertEqual(
            calls,
            [
                ["systemctl", "daemon-reload"],
                ["systemctl", "restart", "onesensor-api.service"],
                ["nginx", "-t", "-c", config["nginx_config_path"]],
                ["systemctl", "reload", "nginx"],
            ],
        )
        restored = adapter.restore_previous_database_binding()
        self.assertEqual(restored["status"], "PASSED")
        with open(environment, "rb") as stream:
            self.assertEqual(stream.read(), before)

    def test_validation_binding_uses_separate_unit_and_candidate_database(self):
        root, publish, config, _environment = self._fixture()
        source_application = config["validation_application_root"]
        candidate_application = os.path.join(root.name, "candidate-app")
        shutil.copytree(source_application, candidate_application)
        validation_unit = config["validation_systemd_unit_file"]
        validation_environment = config["validation_runtime_environment_file"]
        with open(validation_unit, "rb") as stream:
            before_unit = stream.read()
        with open(validation_environment, "rb") as stream:
            before_environment = stream.read()
        calls = []

        def runner(argv):
            calls.append(list(argv))
            if argv[:2] == ["systemctl", "show"]:
                return CompletedProcess(argv, 0, b"4321\n", b"")
            return CompletedProcess(argv, 0, b"", b"")

        adapter = VfoswindProductionLifecycle(
            publish, config, command_runner=runner
        )
        binding = adapter.bind_validation_candidate(
            candidate_application,
            {
                "host": "db",
                "port": 3306,
                "user": "coverage_user",
                "password": "candidate-secret",
                "database": "coverage_vnext_candidate",
            },
        )
        self.assertEqual(binding["status"], "PASSED")
        with open(validation_unit, encoding="utf-8") as stream:
            rebound_unit = stream.read()
        self.assertIn("WorkingDirectory={}".format(candidate_application), rebound_unit)
        with open(validation_environment, encoding="utf-8") as stream:
            runtime = json.loads(
                parse_environment_file(stream.read())["COVERAGE_MYSQL_JSON"]
            )
        self.assertEqual(runtime["database"], "coverage_vnext_candidate")
        self.assertEqual(runtime["password"], "candidate-secret")
        self.assertEqual(adapter.validation_process_ownership()["pid"], 4321)
        restored = adapter.restore_validation_candidate_binding()
        self.assertEqual(restored["status"], "PASSED")
        with open(validation_unit, "rb") as stream:
            self.assertEqual(stream.read(), before_unit)
        with open(validation_environment, "rb") as stream:
            self.assertEqual(stream.read(), before_environment)
        self.assertEqual(calls, [[
            "systemctl", "show", "onesensor-coverage-validation.service",
            "--property=MainPID", "--value",
        ]])

    def test_wrong_served_root_binding_fails_closed(self):
        _root, publish, config, _environment = self._fixture()
        with open(config["nginx_config_path"], "w", encoding="utf-8") as stream:
            stream.write("location /coverage/ { alias /tmp/stale/reports/; }\n")
        with self.assertRaisesRegex(RuntimeError, "Nginx alias"):
            VfoswindProductionLifecycle(publish, config).preflight()

    def test_flat_bootstrap_accepts_legacy_without_identity_bridge_and_stages_one(self):
        _root, publish, config, _environment = self._fixture(flat=True)
        adapter = VfoswindProductionLifecycle(
            publish, config,
            command_runner=lambda argv: CompletedProcess(argv, 0, b"", b""),
        )
        preflight = adapter.preflight(
            expected_database="coverage_vnext_old",
            runtime_mysql={
                "database": "coverage_vnext_old",
                "user": "coverage_user",
            },
            candidate_ports=[19528],
        )
        self.assertEqual(preflight["legacy_nginx_baseline"]["status"], "PASSED")
        self.assertFalse(preflight["legacy_nginx_baseline"]["auth_bridge_present"])
        self.assertEqual(preflight["auth_bridge"]["status"], "PASSED")
        self.assertIn(
            "proxy_set_header X-Remote-User $remote_user;",
            adapter.bootstrap_plan["managed_nginx"],
        )
        self.assertIn("auth_basic Coverage;", adapter.bootstrap_plan["managed_nginx"])

    def test_flat_bootstrap_with_candidate_gateway_does_not_require_legacy_auth(self):
        root, publish, config, _environment = self._fixture(flat=True)
        gateway_path = os.path.join(root.name, "candidate-gateway.conf")
        gateway_reports = os.path.join(publish, "VALIDATION_CURRENT", "reports")
        browser_url = "https://candidate.example.invalid/coverage/report.html"
        with open(gateway_path, "w", encoding="utf-8") as stream:
            stream.write(
                "server {{\n"
                "  location /coverage/ {{\n"
                "    alias {}/;\n"
                "    try_files $uri $uri/ =404;\n"
                "  }}\n"
                "  location /api/coverage {{\n"
                "    auth_basic Coverage;\n"
                "    auth_basic_user_file /etc/nginx/.coverage_htpasswd;\n"
                "    proxy_pass http://127.0.0.1:19528;\n"
                "    proxy_set_header X-Remote-User $remote_user;\n"
                "  }}\n"
                "}}\n".format(gateway_reports)
            )
        config["candidate_gateway"] = {
            "config_path": gateway_path,
            "browser_url": browser_url,
            "static_location": "/coverage/",
            "api_location": "/api/coverage",
            "reports_root": gateway_reports,
            "proxy_pass": "http://127.0.0.1:19528",
        }
        with open(config["nginx_config_path"], encoding="utf-8") as stream:
            legacy_nginx = stream.read()
        self.assertNotIn("auth_basic", legacy_nginx)
        adapter = VfoswindProductionLifecycle(
            publish, config,
            command_runner=lambda argv: CompletedProcess(argv, 0, b"", b""),
        )
        preflight = adapter.preflight(
            expected_database="coverage_vnext_old",
            runtime_mysql={
                "database": "coverage_vnext_old",
                "user": "coverage_user",
            },
            candidate_ports=[19528],
            candidate_gateway_required=True,
            candidate_browser_url=browser_url,
            auth_config={
                "mode": "reverse_proxy",
                "user_header": "X-Remote-User",
                "trusted_proxy_addresses": ["127.0.0.1"],
            },
        )
        self.assertEqual(preflight["status"], "PASSED")
        self.assertEqual(preflight["candidate_gateway"]["status"], "PASSED")
        self.assertFalse(preflight["legacy_nginx_baseline"]["auth_bridge_present"])
        self.assertEqual(preflight["auth_bridge"]["status"], "PASSED")

    def test_identity_bridge_in_static_location_does_not_protect_api_location(self):
        _root, publish, config, _environment = self._fixture()
        nginx = (
            "location /coverage/ {{\n"
            "  alias {}/;\n"
            "  auth_basic Coverage;\n"
            "  auth_basic_user_file /etc/nginx/.coverage_htpasswd;\n"
            "  proxy_set_header X-Remote-User $remote_user;\n"
            "}}\n"
            "location /api/coverage {{\n"
            "  proxy_pass http://127.0.0.1:9528;\n"
            "}}\n"
        ).format(config["legacy_served_root"])
        with self.assertRaisesRegex(RuntimeError, "auth_basic"):
            VfoswindProductionLifecycle(publish, config)._validate_auth_bridge(
                nginx, "production", "/api/coverage"
            )

    def test_candidate_gateway_is_external_static_api_and_pointer_is_restorable(self):
        root, publish, config, _environment = self._fixture()
        gateway_path = os.path.join(root.name, "candidate-gateway.conf")
        gateway_reports = os.path.join(publish, "VALIDATION_CURRENT", "reports")
        browser_url = "https://candidate.example.invalid/coverage/report.html"
        with open(gateway_path, "w", encoding="utf-8") as stream:
            stream.write(
                "server {{\n"
                "  listen 127.0.0.1:19531;\n"
                "  location /coverage/ {{\n"
                "    alias {}/;\n"
                "    try_files $uri $uri/ =404;\n"
                "  }}\n"
                "  location /api/coverage {{\n"
                "    auth_basic Coverage;\n"
                "    auth_basic_user_file /etc/nginx/.coverage_htpasswd;\n"
                "    proxy_pass http://127.0.0.1:19528;\n"
                "    proxy_set_header X-Remote-User $remote_user;\n"
                "  }}\n"
                "}}\n".format(gateway_reports)
            )
        config["candidate_gateway"] = {
            "config_path": gateway_path,
            "browser_url": browser_url,
            "static_location": "/coverage/",
            "api_location": "/api/coverage",
            "reports_root": gateway_reports,
            "proxy_pass": "http://127.0.0.1:19528",
        }
        calls = []

        def runner(argv):
            calls.append(list(argv))
            return CompletedProcess(argv, 0, b"", b"")

        adapter = VfoswindProductionLifecycle(
            publish, config, command_runner=runner
        )
        preflight = adapter.preflight(
            candidate_ports=[19528],
            candidate_gateway_required=True,
            candidate_browser_url=browser_url,
            auth_config={
                "mode": "reverse_proxy",
                "user_header": "X-Remote-User",
                "trusted_proxy_addresses": ["127.0.0.1"],
            },
        )
        self.assertEqual(preflight["candidate_gateway"]["status"], "PASSED")
        self.assertEqual(
            preflight["candidate_gateway"]["auth_bridge"]["identity_expression"],
            "$remote_user",
        )
        release = os.path.join(publish, "releases", "candidate-session")
        os.makedirs(os.path.join(release, "reports"))
        binding = adapter.bind_validation_gateway(
            release, "candidate-session", candidate_ports=[19528],
            browser_url=browser_url,
        )
        self.assertEqual(binding["status"], "PASSED")
        self.assertTrue(os.path.islink(adapter.validation_current))
        self.assertEqual(
            os.path.realpath(adapter.validation_current),
            os.path.realpath(release),
        )
        restored = adapter.restore_validation_gateway()
        self.assertEqual(restored["status"], "PASSED")
        self.assertFalse(os.path.lexists(adapter.validation_current))

    def test_flat_preflight_and_binding_transition_are_explicit_and_restorable(self):
        _root, publish, config, _environment = self._fixture(flat=True)
        with open(config["systemd_unit_file"], "rb") as stream:
            before_unit = stream.read()
        with open(config["nginx_config_path"], "rb") as stream:
            before_nginx = stream.read()

        calls = []

        def runner(argv):
            calls.append(list(argv))
            return CompletedProcess(argv, 0, b"", b"")

        adapter = VfoswindProductionLifecycle(
            publish, config, command_runner=runner
        )
        preflight = adapter.preflight(
            expected_database="coverage_vnext_old",
            runtime_mysql={
                "database": "coverage_vnext_old",
                "user": "coverage_user",
            },
            candidate_ports=[19528],
        )
        self.assertEqual(preflight["deployment_layout"], "FLAT")
        self.assertTrue(preflight["transition_required"])
        self.assertTrue(preflight["bootstrap_ready"])
        self.assertTrue(preflight["rollback_bytes_verified"])
        self.assertEqual(
            preflight["nginx_test"]["probe_mode"],
            "temporary_main_include",
        )
        self.assertEqual(
            preflight["nginx_test"]["staged_config_sha256"],
            preflight["managed_nginx_sha256"],
        )
        self.assertIn(["systemd-analyze", "verify"], [call[:2] for call in calls])
        self.assertTrue(any(
            call[:2] == ["nginx", "-t"] and "-c" in call
            for call in calls
        ))
        with open(config["systemd_unit_file"], "rb") as stream:
            self.assertEqual(stream.read(), before_unit)
        with open(config["nginx_config_path"], "rb") as stream:
            self.assertEqual(stream.read(), before_nginx)
        validation_candidate = os.path.join(_root.name, "validation-candidate")
        shutil.copytree(config["validation_application_root"], validation_candidate)
        validation_binding = adapter.bind_validation_candidate(
            validation_candidate,
            {
                "database": "coverage_vnext_candidate",
                "user": "coverage_user",
            },
        )
        self.assertTrue(validation_binding["bootstrap_validation"]["installed"])
        self.assertTrue(os.path.isfile(config["validation_systemd_unit_file"]))
        validation_restore = adapter.restore_validation_candidate_binding()
        self.assertEqual(validation_restore["status"], "PASSED")
        self.assertFalse(os.path.lexists(config["validation_systemd_unit_file"]))
        self.assertFalse(os.path.lexists(config["validation_runtime_environment_file"]))
        self.assertFalse(os.path.lexists(config["validation_config_path"]))
        release = os.path.join(publish, "releases", "candidate")
        os.makedirs(os.path.join(release, "app", "app"))
        os.makedirs(os.path.join(release, "app", "web"))
        os.makedirs(os.path.join(release, "app", "contracts"))
        os.makedirs(os.path.join(release, "app", "scripts", "compat"))
        os.makedirs(os.path.join(release, "reports"))
        for relative in (
                "app/enhance_coverage.py", "app/app/__init__.py",
                "app/app/bootstrap.py", "app/scripts/compat/git"):
            path = os.path.join(release, relative)
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("#!/bin/sh\n" if relative.endswith("/git") else "")
        os.chmod(os.path.join(release, "app", "scripts", "compat", "git"), 0o755)
        os.symlink(os.path.join("releases", "candidate"),
                   os.path.join(publish, "CURRENT"))
        transitioned = adapter.bind_current_release()
        self.assertEqual(transitioned["status"], "PASSED")
        candidate_binding = adapter.bind_candidate_database({
            "database": "coverage_vnext_candidate",
            "user": "coverage_user",
            "password": "candidate-secret",
        })
        self.assertEqual(candidate_binding["status"], "PASSED")
        with open(config["systemd_unit_file"], encoding="utf-8") as stream:
            unit = stream.read()
        with open(config["nginx_config_path"], encoding="utf-8") as stream:
            nginx = stream.read()
        self.assertIn("WorkingDirectory={}/CURRENT/app".format(publish), unit)
        self.assertIn("alias {}/CURRENT/reports/;".format(publish), nginx)
        self.assertIn("auth_basic Coverage;", nginx)
        self.assertIn(
            "proxy_set_header X-Remote-User $remote_user;", nginx
        )
        restored = adapter.restore_previous_release_bindings()
        self.assertEqual(restored["status"], "PASSED")
        self.assertEqual(
            restored["runtime_environment_restore"]["reason"],
            "deferred_to_database_binding_rollback",
        )
        database_restored = adapter.restore_previous_database_binding()
        self.assertEqual(database_restored["status"], "PASSED")
        with open(config["systemd_unit_file"], encoding="utf-8") as stream:
            self.assertIn(config["legacy_application_root"], stream.read())
        with open(config["nginx_config_path"], encoding="utf-8") as stream:
            self.assertIn(config["legacy_served_root"], stream.read())
        self.assertFalse(os.path.lexists(config["runtime_environment_file"]))

    def test_flat_bootstrap_rejects_invalid_managed_nginx_before_phase_d(self):
        _root, publish, config, _environment = self._fixture(flat=True)
        calls = []

        def runner(argv):
            calls.append(list(argv))
            if argv[:2] == ["nginx", "-t"]:
                main_path = argv[argv.index("-c") + 1]
                managed_path = os.path.join(
                    os.path.dirname(main_path), "managed.conf"
                )
                with open(managed_path, "r", encoding="utf-8") as stream:
                    managed = stream.read()
                if "INVALID_NGINX_DIRECTIVE" in managed:
                    return CompletedProcess(argv, 1, b"", b"syntax error")
            return CompletedProcess(argv, 0, b"", b"")

        adapter = VfoswindProductionLifecycle(
            publish, config, command_runner=runner
        )
        original = adapter._build_flat_bootstrap_plan

        def invalid_plan(*args, **kwargs):
            plan = original(*args, **kwargs)
            plan["managed_nginx"] = "INVALID_NGINX_DIRECTIVE;\n"
            return plan

        adapter._build_flat_bootstrap_plan = invalid_plan
        with self.assertRaisesRegex(RuntimeError, "nginx_test failed"):
            adapter.preflight(
                expected_database="coverage_vnext_old",
                runtime_mysql={
                    "database": "coverage_vnext_old",
                    "user": "coverage_user",
                },
                candidate_ports=[19528],
            )
        self.assertTrue(any(
            call[:2] == ["nginx", "-t"] and "-c" in call
            for call in calls
        ))


if __name__ == "__main__":
    unittest.main()
