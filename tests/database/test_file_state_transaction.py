"""Live-MySQL integration proof for authoritative/derived progress writes.

The test is opt-in because the normal unit-test jobs do not provision MySQL.
Run it with ``COVERAGE_TEST_MYSQL=1`` and ``COVERAGE_TEST_CONFIG=/path/config``.
"""

import hashlib
import json
import os
import unittest

import enhance_coverage
from app.progress.service import ProgressService


@unittest.skipUnless(
    os.environ.get("COVERAGE_TEST_MYSQL") == "1",
    "set COVERAGE_TEST_MYSQL=1 with a live MySQL configuration to run",
)
class FileStateTransactionIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config_path = os.environ.get("COVERAGE_TEST_CONFIG")
        if not config_path:
            raise unittest.SkipTest("COVERAGE_TEST_CONFIG is required for live MySQL integration")
        with open(config_path, "r", encoding="utf-8") as stream:
            cls.config = json.load(stream)
        cls.project = "tx_integration_{}".format(os.getpid())
        cls.manager = enhance_coverage.DatabaseManager(
            cls.config, exit_on_error=False, init_schema=False
        )
        if not cls.manager.conn:
            raise unittest.SkipTest("live MySQL connection is unavailable")
        cls._cleanup()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "manager", None) and cls.manager.conn:
            cls._cleanup()
            cls.manager.close_thread_connection()

    @classmethod
    def _cleanup(cls):
        cursor = cls.manager.conn.cursor()
        for table in ("coverage_file_state", "coverage_analysis", "coverage_line_index", "coverage_project_state"):
            cursor.execute("DELETE FROM {} WHERE project_name = %s".format(table), (cls.project,))
        cls.manager.conn.commit()
        cursor.close()

    def test_batch_save_rebuilds_file_state_in_same_transaction(self):
        file_path = "src/transaction_fixture.c"
        file_hash = hashlib.md5(file_path.encode("utf-8")).hexdigest()
        records = []
        for line_number in (10, 11):
            records.append({
                "file_path": file_path,
                "file_path_hash": file_hash,
                "source_file_name": "transaction_fixture.c",
                "line_number": line_number,
                "line_text": "fixture();",
                "block_start_line": line_number,
                "block_end_line": line_number,
                "block_type": "single",
                "function_name": "fixture",
                "function_hash": "fixture-hash",
                "code_line_hash": "line-{}".format(line_number),
                "code_occurrence": 1,
            })
        self.assertTrue(self.manager.sync_line_index(self.project, records))

        result = self.manager.save_records_batch(
            self.project,
            file_path,
            [{
                "line_numbers": [10, 11],
                "reviewer": "integration",
                "status": "可覆盖",
                "coverage_method": "fixture",
                "uncovered_reason": "",
            }],
            is_draft=False,
        )
        self.assertEqual(result.get("data_version"), 1)

        summary = ProgressService(self.manager.conn).project_summary(self.project)
        self.assertEqual(summary.get("source"), "coverage_file_state")
        self.assertEqual(summary.get("data_version"), 1)
        self.assertEqual(summary.get("file_state_version"), 1)
        self.assertEqual(summary.get("total_uncovered"), 2)
        self.assertEqual(summary.get("filled_total"), 2)
        self.assertEqual(summary.get("confirmed_total"), 2)


if __name__ == "__main__":
    unittest.main()
