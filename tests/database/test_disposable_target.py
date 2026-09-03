import gzip
import os
import tempfile
import unittest
from subprocess import CompletedProcess
from unittest import mock

from scripts.upgrade.disposable_target import (
    create_disposable_target_from_backup, probe_candidate_connection_access,
    validate_disposable_target_config,
)


class DisposableTargetTest(unittest.TestCase):
    class _ProbeCursor(object):
        def __init__(self):
            self.sql = []

        def execute(self, sql):
            self.sql.append(sql)

        def fetchone(self):
            if self.sql[-1].startswith("SELECT DATABASE"):
                return {
                    "database_name": "coverage_vnext_candidate_801",
                    "current_user": "coverage@db",
                }
            return (1,)

        def fetchall(self):
            return [
                ("GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, "
                 "CREATE TEMPORARY TABLES ON `coverage_vnext_candidate_801`.* "
                 "TO 'coverage'@'db'",)
            ]

        def close(self):
            return None

    class _ProbeConnection(object):
        def __init__(self):
            self.cursor_instance = DisposableTargetTest._ProbeCursor()

        def cursor(self):
            return self.cursor_instance

    def test_supplied_target_connection_is_checked_as_candidate_account(self):
        connection = self._ProbeConnection()
        result = probe_candidate_connection_access(connection, {
            "database": "coverage_vnext_candidate_801",
            "user": "coverage",
            "candidate_grant_host": "db",
        })
        self.assertEqual(result["status"], "PASSED")
        self.assertIn("SHOW GRANTS", connection.cursor_instance.sql)
        self.assertTrue(any(
            sql.startswith("CREATE TEMPORARY TABLE")
            for sql in connection.cursor_instance.sql
        ))

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
            }
            target = {
                "host": "db", "port": 3306, "user": "coverage",
                "database": "coverage_vnext_candidate_801",
            }
            calls = []

            def run_client(_client, _common, _env, sql, database=None):
                calls.append((sql, database))
                if sql.startswith("SHOW TABLES"):
                    return CompletedProcess([], 0, b"coverage_projects\n", b"")
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
            }
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


if __name__ == "__main__":
    unittest.main()
