import gzip
import os
import tempfile
import unittest
from subprocess import CompletedProcess
from unittest import mock

from scripts.upgrade.disposable_target import (
    _grant_and_probe_candidate_access, cleanup_disposable_target,
    create_disposable_target_from_backup, probe_candidate_connection_access,
    validate_disposable_target_config,
)


class DisposableTargetTest(unittest.TestCase):
    class _ProbeCursor(object):
        def __init__(self, current_user="coverage@db", grant_text=None):
            self.sql = []
            self.current_user = current_user
            self.grant_text = grant_text or (
                "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, "
                "CREATE TEMPORARY TABLES ON `coverage_vnext_candidate_801`.* "
                "TO 'coverage'@'db'"
            )

        def execute(self, sql):
            self.sql.append(sql)

        def fetchone(self):
            if self.sql[-1].startswith("SELECT DATABASE"):
                return {
                    "database_name": "coverage_vnext_candidate_801",
                    "current_user": self.current_user,
                }
            return (1,)

        def fetchall(self):
            return [(self.grant_text,)]

        def close(self):
            return None

    class _ProbeConnection(object):
        def __init__(self, current_user="coverage@db", grant_text=None):
            self.cursor_instance = DisposableTargetTest._ProbeCursor(
                current_user=current_user, grant_text=grant_text
            )

        def cursor(self):
            return self.cursor_instance

    def test_supplied_target_connection_is_checked_as_candidate_account(self):
        connection = self._ProbeConnection()
        result = probe_candidate_connection_access(connection, {
            "database": "coverage_vnext_candidate_801",
            "user": "coverage",
            "candidate_grant_host": "db",
            "approved_application_user": "coverage",
            "approved_application_host": "db",
        })
        self.assertEqual(result["status"], "PASSED")
        self.assertIn("SHOW GRANTS", connection.cursor_instance.sql)
        self.assertTrue(any(
            sql.startswith("CREATE TEMPORARY TABLE")
            for sql in connection.cursor_instance.sql
        ))

    def test_supplied_target_connection_requires_exact_user_and_host(self):
        target = {
            "database": "coverage_vnext_candidate_801",
            "user": "coverage_user",
            "candidate_grant_host": "127.0.0.1",
            "approved_application_user": "coverage_user",
            "approved_application_host": "127.0.0.1",
        }
        for observed in (
                "coverage_user_backup@127.0.0.1",
                "coverage_user@localhost",
        ):
            with self.subTest(observed=observed):
                with self.assertRaisesRegex(RuntimeError, "exactly"):
                    probe_candidate_connection_access(
                        self._ProbeConnection(current_user=observed), target
                    )

        exact_grants = (
            "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, "
            "CREATE TEMPORARY TABLES ON `coverage_vnext_candidate_801`.* "
            "TO 'coverage_user'@'127.0.0.1'"
        )
        result = probe_candidate_connection_access(
            self._ProbeConnection(
                current_user="coverage_user@127.0.0.1",
                grant_text=exact_grants,
            ), target
        )
        self.assertEqual(result["status"], "PASSED")

    def _backup(self, root):
        dump = os.path.join(root, "full.sql.gz")
        with gzip.open(dump, "wb") as stream:
            stream.write(b"CREATE TABLE coverage_projects (id INT);\n")
        from scripts.maintenance.mysql_backup import compute_file_sha256
        return dump, {
            "artifact_path": dump,
            "full_sql_gz_sha256": compute_file_sha256(dump),
            "database": "coverage_vnext_e9fcc837",
            "verification": {"table_inventory": ["coverage_projects"]},
        }

    def test_target_name_and_source_alias_fail_closed(self):
        source = {"database": "coverage_vnext_e9fcc837"}
        with self.assertRaisesRegex(ValueError, "prefix"):
            validate_disposable_target_config(
                source, {"database": "production"}
            )
        with self.assertRaisesRegex(ValueError, "differ"):
            validate_disposable_target_config(
                source, {"database": "coverage_vnext_e9fcc837"}
            )
        with self.assertRaisesRegex(ValueError, "may not be root"):
            validate_disposable_target_config(
                source,
                {"database": "coverage_vnext_candidate_801", "user": "root"},
            )

    def test_restore_creates_only_the_new_target_and_retains_it(self):
        with tempfile.TemporaryDirectory(prefix="disposable-target-") as root:
            dump, backup = self._backup(root)
            source = {
                "host": "db", "port": 3306, "user": "coverage",
                "database": "coverage_vnext_e9fcc837",
                "application_grant_host": "db",
            }
            target = {
                "host": "db", "port": 3306, "user": "coverage",
                "database": "coverage_vnext_candidate_801",
                "candidate_grant_host": "db",
                "approved_application_user": "coverage",
                "approved_application_host": "db",
            }
            calls = []

            def run_client(_client, _common, _env, sql, database=None):
                calls.append((sql, database))
                if sql.startswith("SHOW TABLES"):
                    return CompletedProcess([], 0, b"coverage_projects\n", b"")
                if sql.startswith("SELECT User, Host FROM mysql.user"):
                    return CompletedProcess([], 0, b"coverage\tdb\n", b"")
                if sql.startswith("SHOW GRANTS FOR"):
                    return CompletedProcess(
                        [], 0,
                        b"GRANT USAGE ON *.* TO 'coverage'@'db'\n",
                        b"",
                    )
                if sql.startswith("SHOW GRANTS"):
                    return CompletedProcess(
                        [], 0,
                        b"GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, CREATE TEMPORARY TABLES ON `coverage_vnext_candidate_801`.* TO 'coverage'@'db'\n",
                        b"",
                    )
                return CompletedProcess([], 0, b"", b"")

            with mock.patch(
                    "scripts.upgrade.disposable_target._client_settings",
                    side_effect=[("mysql", [], {}), ("mysql", [], {}),
                                 ("mysql", [], {})]), \
                    mock.patch(
                        "scripts.upgrade.disposable_target._run_client",
                        side_effect=run_client), \
                    mock.patch(
                        "scripts.upgrade.disposable_target._runtime_identity",
                        side_effect=[
                            (True, {"database": source["database"]}, ""),
                            (True, {"database": target["database"]}, ""),
                        ]), \
                    mock.patch(
                        "scripts.upgrade.disposable_target._restore_process"
                    ) as restore:
                result = create_disposable_target_from_backup(
                    backup, source, target,
                )

            self.assertEqual(result["status"], "PASSED")
            self.assertTrue(result["target_database_created_by_this_run"])
            self.assertTrue(result["target_retained_for_candidate"])
            restore.assert_called_once_with(
                "mysql", [], {}, dump, target["database"]
            )
            self.assertTrue(any(sql.startswith("CREATE DATABASE") for sql, _ in calls))
            self.assertFalse(any(sql.startswith("DROP DATABASE") for sql, _ in calls))

    def test_existing_target_is_never_reused_or_dropped(self):
        with tempfile.TemporaryDirectory(prefix="disposable-target-existing-") as root:
            dump, backup = self._backup(root)
            source = {"database": "coverage_vnext_e9fcc837"}
            target = {
                "database": "coverage_vnext_candidate_801",
                "user": "coverage",
                "candidate_grant_host": "db",
                "approved_application_user": "coverage",
                "approved_application_host": "db",
            }
            source["application_grant_host"] = "db"
            calls = []

            def run_client(_client, _common, _env, sql, database=None):
                calls.append(sql)
                return CompletedProcess([], 0, b"coverage_vnext_candidate_801\n", b"")

            with mock.patch(
                    "scripts.upgrade.disposable_target._client_settings",
                    side_effect=[("mysql", [], {}), ("mysql", [], {})]), \
                    mock.patch(
                        "scripts.upgrade.disposable_target._run_client",
                        side_effect=run_client):
                with self.assertRaisesRegex(RuntimeError, "already exists"):
                    create_disposable_target_from_backup(backup, source, target)
            self.assertEqual(len(calls), 1)
            self.assertFalse(any(sql.startswith("DROP DATABASE") for sql in calls))

    def _approved_target(self):
        return {
            "database": "coverage_vnext_candidate_801",
            "user": "coverage",
            "candidate_grant_host": "db",
            "approved_application_user": "coverage",
            "approved_application_host": "db",
        }

    def test_nonexistent_candidate_account_fails_before_grant(self):
        calls = []

        def run_client(_client, _common, _env, sql, database=None):
            calls.append(sql)
            if sql.startswith("SELECT User, Host FROM mysql.user"):
                return CompletedProcess([], 0, b"", b"")
            return CompletedProcess([], 0, b"", b"")

        with mock.patch(
                "scripts.upgrade.disposable_target._run_client",
                side_effect=run_client):
            with self.assertRaisesRegex(RuntimeError, "does not exist"):
                _grant_and_probe_candidate_access(
                    "mysql", [], {}, self._approved_target(),
                    "coverage_vnext_candidate_801",
                    source={"user": "coverage", "application_grant_host": "db"},
                )
        self.assertFalse(any(sql.startswith("GRANT ") for sql in calls))

    def test_failed_after_grant_restores_grant_state(self):
        calls = []
        pre_grant = b"GRANT USAGE ON *.* TO 'coverage'@'db'\n"
        target_grant = (
            b"GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, "
            b"CREATE TEMPORARY TABLES ON `coverage_vnext_candidate_801`.* "
            b"TO 'coverage'@'db'\n"
        )

        def run_client(_client, _common, _env, sql, database=None):
            calls.append(sql)
            if sql.startswith("SELECT User, Host FROM mysql.user"):
                return CompletedProcess([], 0, b"coverage\tdb\n", b"")
            if sql.startswith("SHOW GRANTS FOR"):
                return CompletedProcess([], 0, pre_grant, b"")
            if sql.startswith("SHOW GRANTS"):
                return CompletedProcess([], 0, target_grant, b"")
            if sql.startswith("CREATE TEMPORARY TABLE"):
                return CompletedProcess([], 1, b"", b"probe denied")
            return CompletedProcess([], 0, b"", b"")

        with mock.patch(
                "scripts.upgrade.disposable_target._client_settings",
                return_value=("mysql", [], {})), \
                mock.patch(
                    "scripts.upgrade.disposable_target._run_client",
                    side_effect=run_client):
            with self.assertRaisesRegex(RuntimeError, "capability probe failed"):
                _grant_and_probe_candidate_access(
                    "mysql", [], {}, self._approved_target(),
                    "coverage_vnext_candidate_801",
                    source={"user": "coverage", "application_grant_host": "db"},
                )
        self.assertTrue(any(sql.startswith("REVOKE ALL PRIVILEGES") for sql in calls))
        self.assertGreaterEqual(
            sum(sql.startswith("SHOW GRANTS FOR") for sql in calls), 2
        )

    def test_historical_candidate_database_grant_blocks_before_grant(self):
        calls = []
        historical = (
            b"GRANT SELECT ON `coverage_vnext_candidate_801`.* "
            b"TO 'coverage'@'db'\n"
        )

        def run_client(_client, _common, _env, sql, database=None):
            calls.append(sql)
            if sql.startswith("SELECT User, Host FROM mysql.user"):
                return CompletedProcess([], 0, b"coverage\tdb\n", b"")
            if sql.startswith("SHOW GRANTS FOR"):
                return CompletedProcess([], 0, historical, b"")
            return CompletedProcess([], 0, b"", b"")

        with mock.patch(
                "scripts.upgrade.disposable_target._run_client",
                side_effect=run_client):
            with self.assertRaisesRegex(RuntimeError, "historical grant"):
                _grant_and_probe_candidate_access(
                    "mysql", [], {}, self._approved_target(),
                    "coverage_vnext_candidate_801",
                    source={"user": "coverage", "application_grant_host": "db"},
                )
        self.assertEqual(
            sum(sql.startswith("GRANT ") for sql in calls), 0
        )

    def test_historical_candidate_database_grant_blocks_before_create(self):
        with tempfile.TemporaryDirectory(prefix="disposable-target-grant-") as root:
            dump, backup = self._backup(root)
            source = {
                "database": "coverage_vnext_e9fcc837",
                "user": "coverage",
                "application_grant_host": "db",
            }
            target = self._approved_target()
            calls = []
            historical = (
                b"GRANT SELECT ON `coverage_vnext_candidate_801`.* "
                b"TO 'coverage'@'db'\n"
            )

            def run_client(_client, _common, _env, sql, database=None):
                calls.append(sql)
                if sql.startswith("SELECT SCHEMA_NAME"):
                    return CompletedProcess([], 0, b"", b"")
                if sql.startswith("SELECT User, Host FROM mysql.user"):
                    return CompletedProcess([], 0, b"coverage\tdb\n", b"")
                if sql.startswith("SHOW GRANTS FOR"):
                    return CompletedProcess([], 0, historical, b"")
                return CompletedProcess([], 0, b"", b"")

            with mock.patch(
                    "scripts.upgrade.disposable_target._client_settings",
                    return_value=("mysql", [], {})), \
                    mock.patch(
                        "scripts.upgrade.disposable_target._run_client",
                        side_effect=run_client), \
                    mock.patch(
                        "scripts.upgrade.disposable_target._restore_process"
                    ) as restore:
                with self.assertRaisesRegex(RuntimeError, "historical grant"):
                    create_disposable_target_from_backup(
                        backup, source, target,
                    )

            self.assertFalse(any(sql.startswith("CREATE DATABASE") for sql in calls))
            restore.assert_not_called()

    def test_wrong_candidate_grant_host_is_rejected(self):
        source = {
            "database": "coverage_vnext_e9fcc837",
            "user": "coverage",
            "application_grant_host": "127.0.0.1",
        }
        target = self._approved_target()
        target["candidate_grant_host"] = "localhost"
        with self.assertRaisesRegex(ValueError, "principal does not match"):
            validate_disposable_target_config(source, target)

    def test_cleanup_restores_account_then_drops_only_new_target(self):
        calls = []
        pre_grant = b"GRANT USAGE ON *.* TO 'coverage'@'db'\n"

        def run_client(_client, _common, _env, sql, database=None):
            calls.append(sql)
            if sql.startswith("SELECT User, Host FROM mysql.user"):
                return CompletedProcess([], 0, b"coverage\tdb\n", b"")
            if sql.startswith("SHOW GRANTS FOR"):
                return CompletedProcess([], 0, pre_grant, b"")
            if sql.startswith("SELECT SCHEMA_NAME"):
                return CompletedProcess([], 0, b"", b"")
            return CompletedProcess([], 0, b"", b"")

        evidence = {
            "target_database_created_by_this_run": True,
            "target_database": "coverage_vnext_candidate_801",
            "candidate_access": {
                "grant_applied_by_run": True,
                "pre_grant_privilege_snapshot": {
                    "grants": ["GRANT USAGE ON *.* TO 'coverage'@'db'"]
                },
            },
        }
        with mock.patch(
                "scripts.upgrade.disposable_target._client_settings",
                return_value=("mysql", [], {})), \
                mock.patch(
                    "scripts.upgrade.disposable_target._run_client",
                    side_effect=run_client):
            result = cleanup_disposable_target(self._approved_target(), evidence)
        self.assertEqual(result["status"], "PASSED")
        self.assertTrue(any(sql.startswith("REVOKE ALL PRIVILEGES") for sql in calls))
        self.assertTrue(any(sql.startswith("DROP DATABASE") for sql in calls))


if __name__ == "__main__":
    unittest.main()
