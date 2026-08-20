#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Unit tests for scripts/maintenance/clear_coverage_data.py."""

import unittest
from unittest import mock

from scripts.maintenance import clear_coverage_data


class TestClearCoverageData(unittest.TestCase):

    def test_get_arg_value_and_has_arg(self):
        args = ["--project", "my_proj", "--yes"]
        self.assertEqual(clear_coverage_data.get_arg_value(args, "--project"), "my_proj")
        self.assertIsNone(clear_coverage_data.get_arg_value(args, "--missing"))
        self.assertTrue(clear_coverage_data.has_arg(args, "--yes"))
        self.assertFalse(clear_coverage_data.has_arg(args, "--all"))

    def test_table_count_queries(self):
        mock_cursor = mock.MagicMock()
        mock_cursor.fetchone.return_value = (42,)

        cnt1 = clear_coverage_data.table_count(mock_cursor, "coverage_analysis", "test_proj")
        self.assertEqual(cnt1, 42)
        mock_cursor.execute.assert_called_with(
            "SELECT COUNT(*) FROM coverage_analysis WHERE project_name = %s", ("test_proj",)
        )

        mock_cursor.fetchone.return_value = {"count": 100}
        cnt2 = clear_coverage_data.table_count(mock_cursor, "coverage_line_index")
        self.assertEqual(cnt2, 100)

    def test_main_validation_fails_without_yes(self):
        with mock.patch("sys.argv", ["clear_coverage_data.py", "--project", "test_proj"]):
            ret = clear_coverage_data.main()
            self.assertEqual(ret, 1)

    def test_main_validation_fails_with_conflicting_args(self):
        with mock.patch("sys.argv", ["clear_coverage_data.py", "--all", "--project", "test_proj", "--yes"]):
            ret = clear_coverage_data.main()
            self.assertEqual(ret, 1)

    def test_main_clears_single_project_data_successfully(self):
        mock_conn = mock.MagicMock()
        mock_cursor = mock.MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = (0,)
        mock_cursor.fetchall.return_value = []

        mock_db_mgr = mock.MagicMock()
        mock_db_mgr.conn = mock_conn

        with mock.patch("sys.argv", ["clear_coverage_data.py", "--project", "proj_a", "--yes"]):
            with mock.patch.object(clear_coverage_data, "load_config", return_value={}):
                with mock.patch.object(clear_coverage_data, "DatabaseManager", return_value=mock_db_mgr):
                    ret = clear_coverage_data.main()

        self.assertEqual(ret, 0)
        self.assertTrue(mock_conn.commit.called)


if __name__ == "__main__":
    unittest.main()
