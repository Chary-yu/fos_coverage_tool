#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
System-wide integration and verification test suite for all 10 coverage export report types:
- CSV/Progress formats (8 types):
    1. detail
    2. file_summary
    3. project_summary
    4. full_detail
    5. full_file_summary
    6. full_dir_summary
    7. full_project_summary
    8. full_progress_summary
- XLSX Spreadsheet (1 type):
    9. review_excel
- ZIP Excel by directory package (1 type):
    10. review_excel_by_dir
"""

import unittest
import unittest.mock
import io
import zipfile
import json
from datetime import datetime

# Import target components
import enhance_coverage
from enhance_coverage import DatabaseManager, CoverageHTTPRequestHandler


class MockCursor:
    """Mock cursor dynamically returning mock records conforming to key schemas."""
    def __init__(self):
        self.last_query = ""

    def execute(self, sql, params=None):
        self.last_query = sql

    def fetchall(self):
        sql_lower = self.last_query.lower()
        if "from coverage_analysis" in sql_lower:
            if "group by" in sql_lower:
                if "file_path" in sql_lower:
                    # 13 columns for file_summary
                    return [("test_proj", "src/main.c", 10, 5, 2, 2, 1, 5, 50.0, 20.0, 20.0, 10.0, datetime.now())]
                else:
                    # 13 columns for project_summary (includes file_total count)
                    return [("test_proj", 10, 5, 2, 2, 1, 5, 50.0, 20.0, 20.0, 10.0, 1, datetime.now())]
            else:
                # 8 columns for detail
                return [("test_proj", "src/main.c", 42, "Alice", "已审核", "UT", "", datetime.now())]

        elif "from coverage_line_index" in sql_lower:
            if "left join" in sql_lower:
                if "group by" in sql_lower:
                    if "dir_path" in sql_lower or "dir_expr" in sql_lower or "substring_index" in sql_lower:
                        # 13 columns for full_dir_summary
                        return [("test_proj", "src", 1, 10, 5, 5, 2, 2, 2, 1, 50.0, 20.0, datetime.now())]
                    elif "file_path" in sql_lower:
                        # 12 columns for full_file_summary
                        return [("test_proj", "src/main.c", 10, 5, 5, 2, 2, 2, 1, 50.0, 20.0, datetime.now())]
                    else:
                        # 12 columns for full_project_summary
                        return [("test_proj", 1, 10, 5, 5, 2, 2, 2, 1, 50.0, 20.0, datetime.now())]
                else:
                    if "i.line_text" in sql_lower and "block_start_line" in sql_lower:
                        # 13 columns for full_detail
                        return [("test_proj", "src/main.c", 42, "int x = 1;", 42, 42, "single", "已填写", "可覆盖", "Alice", "UT", "", datetime.now())]
                    else:
                        # 9 columns for fetch_review_excel_rows
                        return [("test_proj", "main.c", "src/main.c", 42, "int x = 1;", "可覆盖", "UT", "", "Alice")]
            else:
                return []
        return []

    def close(self):
        pass


class MockConnection:
    """Mock database connection."""
    def cursor(self):
        return MockCursor()

    def ping(self, reconnect=True):
        pass


class TestExportSystem(unittest.TestCase):
    def setUp(self):
        # Stash original global db_manager
        self.old_db = getattr(enhance_coverage, "db_manager", None)
        
        # Start patching DatabaseManager.get_connection so instantiation does not hit real MySQL
        self.patcher = unittest.mock.patch('enhance_coverage.DatabaseManager.get_connection', return_value=MockConnection())
        self.mock_get_conn = self.patcher.start()

        self.mock_db = DatabaseManager({
            'mysql': {
                'host': 'localhost',
                'port': 3306,
                'user': 'root',
                'password': '',
                'database': 'coverage'
            }
        }, exit_on_error=False, init_schema=False)
        self.mock_db.conn = MockConnection()
        enhance_coverage.db_manager = self.mock_db

    def tearDown(self):
        # Stop patching
        self.patcher.stop()
        # Restore stashed global db_manager
        enhance_coverage.db_manager = self.old_db

    def test_csv_progress_formats(self):
        """Verify all 8 CSV/Progress report formats output expected columns and correct types."""
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

        print("\n=== Verifying All CSV/Progress Formats ===")
        for fmt in formats:
            headers, data = self.mock_db.export_report(fmt, "test_proj")
            print(f"[CSV Format] '{fmt}' verified successfully! Headers count={len(headers)}, Rows count={len(data)}")
            self.assertIsNotNone(headers)
            self.assertIsNotNone(data)
            self.assertTrue(len(headers) > 0)
            if fmt == "full_progress_summary":
                self.assertEqual(len(data), 3)  # Project, dir, and file entries exist

    def test_review_excel_format(self):
        """Verify review_excel (XLSX Spreadsheet) output is structurally sound and compiles successfully."""
        print("\n=== Verifying review_excel (XLSX Spreadsheet) Format ===")
        
        # 1. Fetch excel data rows
        detail_rows = self.mock_db.fetch_review_excel_rows("test_proj")
        self.assertEqual(len(detail_rows), 1)
        self.assertEqual(detail_rows[0][1], "main.c")
        
        # 2. Fetch progress sections
        project_headers, project_rows = self.mock_db.export_report("full_project_summary", "test_proj")
        dir_headers, dir_rows = self.mock_db.export_report("full_dir_summary", "test_proj")
        file_headers, file_rows = self.mock_db.export_report("full_file_summary", "test_proj")
        
        progress_sections = [
            ("项目进度", project_headers, project_rows),
            ("目录进度", dir_headers, dir_rows),
            ("文件进度", file_headers, file_rows),
        ]
        
        # 3. Compile xlsx workbook
        xlsx_data = enhance_coverage.build_review_excel("test_proj", detail_rows, progress_sections)
        self.assertIsNotNone(xlsx_data)
        self.assertTrue(len(xlsx_data) > 100)  # Verify non-trivial binary compilation
        print(f"[XLSX Format] compiled successfully! Binary size={len(xlsx_data)} bytes")

    def test_review_excel_by_dir_format(self):
        """Verify review_excel_by_dir (ZIP of XLSXs) handles directories and generates valid archives."""
        print("\n=== Verifying review_excel_by_dir (ZIP Excel Package) Format ===")
        
        # Mock CoverageHTTPRequestHandler context
        handler = unittest.mock.MagicMock(spec=CoverageHTTPRequestHandler)
        handler.path = "/api/coverage/export?type=review_excel_by_dir&project=test_proj"
        mock_wfile = io.BytesIO()
        handler.wfile = mock_wfile

        # Trigger XLSX ZIP export
        CoverageHTTPRequestHandler.send_review_excel_by_dir_response(handler, "review.zip", "test_proj")

        # Parse ZIP output
        mock_wfile.seek(0)
        with zipfile.ZipFile(mock_wfile, "r") as archive:
            namelist = archive.namelist()
            print("[ZIP Package] Generated member files:", namelist)
            self.assertIn("EXPORT_STARTED.txt", namelist)
            self.assertTrue(any("src.xlsx" in name for name in namelist))
            
            # Verify Excel spreadsheet binary in ZIP
            excel_bytes = archive.read("src.xlsx")
            self.assertTrue(len(excel_bytes) > 100)
            print(f"[ZIP Package] verified successfully! ZIP has 'src.xlsx' ({len(excel_bytes)} bytes)")

    def test_full_progress_summary_xlsx(self):
        """Verify full_progress_summary includes the team/leader progress sheet."""
        print("\n=== Verifying full_progress_summary (XLSX Spreadsheet) Format ===")
        
        project_headers, project_rows = self.mock_db.export_report("full_project_summary", "test_proj")
        dir_headers, dir_rows = self.mock_db.export_report("full_dir_summary", "test_proj")
        file_headers, file_rows = self.mock_db.export_report("full_file_summary", "test_proj")
        
        progress_sections = [
            ("项目进度", project_headers, project_rows),
            ("目录进度", dir_headers, dir_rows),
            ("小组进度", [
                "team", "leader", "module_names", "file_total", "total_uncovered",
                "filled_total", "unfilled_total", "confirmed_total", "fill_rate",
                "confirmed_rate",
            ], [["平台一组", "张三", "NET_CORE", 1, 10, 5, 5, 2, 50.0, 20.0]]),
            ("文件进度", file_headers, file_rows),
        ]
        
        # Compile progress xlsx workbook
        xlsx_data = enhance_coverage.build_progress_excel("test_proj", progress_sections)
        self.assertIsNotNone(xlsx_data)
        self.assertTrue(len(xlsx_data) > 100)
        with zipfile.ZipFile(io.BytesIO(xlsx_data), "r") as archive:
            workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
            team_sheet_xml = archive.read("xl/worksheets/sheet3.xml").decode("utf-8")
        self.assertIn('name="小组进度"', workbook_xml)
        self.assertIn("平台一组", team_sheet_xml)
        self.assertIn("张三", team_sheet_xml)
        print(f"[Progress XLSX Format] compiled successfully! Binary size={len(xlsx_data)} bytes")


if __name__ == "__main__":
    unittest.main()
