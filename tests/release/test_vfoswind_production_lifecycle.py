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
        self.assertFalse(integration["bootstrap"]["nginx_test_uses_staged_path"])

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
            json.dump({"server": {"port": 19528}}, stream)
        with open(nginx, "w", encoding="utf-8") as stream:
            stream.write(
                "location /coverage/ {{\n"
                "  alias {}/;\n"
                "  proxy_pass http://127.0.0.1:9528;\n"
                "}}\n".format(current_reports)
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
            "legacy_application_root": current_app,
            "legacy_served_root": current_reports,
            "bootstrap": {
                "legacy_systemd_unit": "onesensor-api.service",
                "legacy_systemd_unit_file": unit,
                "legacy_nginx_config_path": nginx,
                "systemd_analyze": ["systemd-analyze", "verify"],
                "nginx_test": ["nginx", "-t"],
                "nginx_test_uses_staged_path": False,
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
        self.assertIn(["systemd-analyze", "verify"], [call[:2] for call in calls])
        self.assertIn(["nginx", "-t"], calls)
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


if __name__ == "__main__":
    unittest.main()
