#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Performance and query count verification test for Optimized Directory Excel Exporter.
"""

import unittest
import unittest.mock
import io
import zipfile

# Import target components
import enhance_coverage
from enhance_coverage import CoverageHTTPRequestHandler


class MockDatabaseManager:
    """Mock database manager designed to count query calls and return mock project rows."""
    def __init__(self, config):
        self.fetch_count = 0

    def fetch_review_excel_rows(self, project_name, dir_path=None):
        self.fetch_count += 1
        # Return mock detail rows spanning 2 different directories
        return [
            # project_name, source_file_name, file_path, line_number, line_text, status, method, reason, reviewer
            ("perf_proj", "file1.c", "src/dir1/file1.c", 10, "int x = 1;", "可覆盖", "UT", "", "Alice"),
            ("perf_proj", "file2.c", "src/dir2/file2.c", 20, "int y = 2;", "未确认", "", "", ""),
        ]

    def export_report(self, report_type, project_name):
        # Mock summaries returning consistent schemas
        if report_type == "full_project_summary":
            return ["project_name", "total_uncovered"], [["perf_proj", 2]]
        elif report_type == "full_dir_summary":
            return ["project_name", "dir_path", "total_uncovered"], [
                ["perf_proj", "src/dir1", 1],
                ["perf_proj", "src/dir2", 1]
            ]
        elif report_type == "full_file_summary":
            return ["project_name", "file_path", "total_uncovered"], [
                ["perf_proj", "src/dir1/file1.c", 1],
                ["perf_proj", "src/dir2/file2.c", 1]
            ]
        return [], []


class TestExportPerformance(unittest.TestCase):
    def test_single_query_pre_fetch(self):
        # Stash original global db_manager
        old_db = getattr(enhance_coverage, "db_manager", None)

        mock_db = MockDatabaseManager(None)

        class MockProxy:
            def __getattr__(self, name):
                return getattr(mock_db, name)

        enhance_coverage.db_manager = MockProxy()

        try:
            # Set up mock request handler
            handler = unittest.mock.MagicMock(spec=CoverageHTTPRequestHandler)
            handler.path = "/api/coverage/export?type=review_excel_by_dir&project=perf_proj"

            # Mock wfile as a BytesIO stream
            mock_wfile = io.BytesIO()
            handler.wfile = mock_wfile

            # Execute the export response
            CoverageHTTPRequestHandler.send_review_excel_by_dir_response(handler, "review.zip", "perf_proj")

            # Parse and verify the ZIP structure
            mock_wfile.seek(0)
            with zipfile.ZipFile(mock_wfile, "r") as archive:
                namelist = archive.namelist()
                print("\n[Performance Verification] Generated ZIP files:", namelist)
                self.assertIn("EXPORT_STARTED.txt", namelist)
                # Verify that files are correctly grouped and named based on source directory
                self.assertTrue(any("src__dir1.xlsx" in name for name in namelist))
                self.assertTrue(any("src__dir2.xlsx" in name for name in namelist))

            # Verify the database query count:
            # fetch_review_excel_rows MUST be called exactly ONCE (no N+1 scans)!
            print(f"[Performance Verification] fetch_review_excel_rows called: {mock_db.fetch_count} time(s).")
            self.assertEqual(
                mock_db.fetch_count,
                1,
                "fetch_review_excel_rows was called more than once (N+1 query bottleneck exists!)"
            )
            print("[Performance Verification] Pre-fetch optimization successfully verified!")

        finally:
            # Restore stashed global db_manager
            enhance_coverage.db_manager = old_db


if __name__ == "__main__":
    unittest.main()
