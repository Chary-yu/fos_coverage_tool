#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Automated unit and integration test suite to verify all report export formats,
specifically testing the recursive full_progress_summary structure.
"""

import unittest
import unittest.mock

# Import target components
import enhance_coverage
from enhance_coverage import DatabaseManager


class MockCursor:
    """Mock cursor dynamically returning data tuples matching each format's specific schema."""
    def __init__(self):
        self.last_query = ""

    def execute(self, sql, params=None):
        self.last_query = sql

    def fetchall(self):
        if "dir_path" in self.last_query:
            # 13 columns for full_dir_summary
            return [("test_proj", "src/dir", 10, 5, 5, 2, 2, 2, 1, 50.0, 20.0, "2026-05-31 12:00:00", "2026-05-31 12:00:00")]
        elif "fill_status" in self.last_query:
            # 13 columns for full_detail
            return [("test_proj", "src/file.c", 10, "int x = 1;", 10, 10, "single", "已填写", "可覆盖", "Alice", "UT", "", "2026-05-31 12:00:00")]
        elif "review_total" in self.last_query:
            # 13 columns for file_summary/project_summary
            return [("test_proj", "src/file.c", 10, 5, 2, 2, 1, 5, 50.0, 20.0, 20.0, 10.0, "2026-05-31 12:00:00")]
        else:
            # 12 columns for project_summary or standard file-level queries
            return [("test_proj", "src/file.c", 10, 5, 5, 2, 2, 2, 1, 50.0, 20.0, "2026-05-31 12:00:00")]

    def close(self):
        pass


class MockConnection:
    """Mock database connection."""
    def cursor(self):
        return MockCursor()

    def ping(self, reconnect=True):
        pass


class TestExportFormats(unittest.TestCase):
    @unittest.mock.patch('enhance_coverage.DatabaseManager.get_connection', return_value=MockConnection())
    def test_all_export_formats(self, mock_get_conn):
        # Instantiate DB Manager with mocked connection
        db = DatabaseManager({
            'mysql': {
                'host': 'localhost',
                'port': 3306,
                'user': 'root',
                'password': '',
                'database': 'coverage'
            }
        }, exit_on_error=False, init_schema=False)
        db.conn = MockConnection()

        # List of all report formats supported by export_report
        formats = [
            "detail",
            "file_summary",
            "project_summary",
            "full_detail",
            "full_file_summary",
            "full_dir_summary",
            "full_project_summary",
            "full_progress_summary"
        ]

        print("\n=== Testing Export Formats ===")
        for fmt in formats:
            try:
                headers, data = db.export_report(fmt, "test_proj")
                print(f"[Test Format] Format '{fmt}' succeeded! Columns={len(headers)}, Rows={len(data)}")
                
                # Check structures
                self.assertIsNotNone(headers)
                self.assertIsNotNone(data)
                self.assertTrue(len(headers) > 0)
                
                # Special checks for the multi-level progress summary format
                if fmt == "full_progress_summary":
                    self.assertEqual(len(data), 3)  # Contains project, dir, and file entries
                    
            except Exception as e:
                self.fail(f"Format '{fmt}' failed with exception: {e}")


if __name__ == "__main__":
    unittest.main()
