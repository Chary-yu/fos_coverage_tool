#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Unit and integration test suite for enhance_coverage.py.
"""

import unittest
import unittest.mock
import os
import shutil
import tempfile
import re
import json

# Import functions from enhance_coverage.py
import enhance_coverage


class TestCParserHelpers(unittest.TestCase):
    """Test unit-level parser helper functions for C syntax flow detection."""

    def test_strip_html_text(self):
        self.assertEqual(
            enhance_coverage.strip_html_text("<span class=\"lineNum\"> 12 </span>"),
            "12"
        )
        self.assertEqual(
            enhance_coverage.strip_html_text("int a &amp;&amp; b;"),
            "int a && b;"
        )

    def test_get_code_text(self):
        self.assertEqual(enhance_coverage.get_code_text("15: int a = 1;"), "int a = 1;")
        self.assertEqual(enhance_coverage.get_code_text("int b = 2;"), "int b = 2;")
        self.assertEqual(enhance_coverage.get_code_text("  5 :  c = 3;  "), "c = 3;")

    def test_is_control_flow_text(self):
        self.assertTrue(enhance_coverage.is_control_flow_text("if (a == 1)"))
        self.assertTrue(enhance_coverage.is_control_flow_text("} else {"))
        self.assertTrue(enhance_coverage.is_control_flow_text("for (int i = 0; i < n; i++)"))
        self.assertTrue(enhance_coverage.is_control_flow_text("while (condition)"))
        self.assertTrue(enhance_coverage.is_control_flow_text("switch (val)"))
        self.assertTrue(enhance_coverage.is_control_flow_text("case 1:"))
        self.assertTrue(enhance_coverage.is_control_flow_text("default:"))
        self.assertFalse(enhance_coverage.is_control_flow_text("int a = 5;"))

    def test_is_function_entry_text(self):
        self.assertTrue(enhance_coverage.is_function_entry_text("void func(int x) {"))
        self.assertTrue(enhance_coverage.is_function_entry_text("int main(void)"))
        self.assertFalse(enhance_coverage.is_function_entry_text("if (a == 1)"))
        self.assertFalse(enhance_coverage.is_function_entry_text("return 0;"))

    def test_is_jump_text(self):
        self.assertTrue(enhance_coverage.is_jump_text("return 0;"))
        self.assertTrue(enhance_coverage.is_jump_text("goto error;"))
        self.assertTrue(enhance_coverage.is_jump_text("break;"))
        self.assertTrue(enhance_coverage.is_jump_text("continue;"))
        self.assertFalse(enhance_coverage.is_jump_text("int x = 5;"))

    def test_is_structural_text(self):
        self.assertTrue(enhance_coverage.is_structural_text("{"))
        self.assertTrue(enhance_coverage.is_structural_text("}"))
        self.assertTrue(enhance_coverage.is_structural_text("};"))
        self.assertTrue(enhance_coverage.is_structural_text(""))
        self.assertFalse(enhance_coverage.is_structural_text("int x = 5;"))

    def test_is_simple_auto_group_text(self):
        self.assertTrue(enhance_coverage.is_simple_auto_group_text("x = 5;"))
        self.assertTrue(enhance_coverage.is_simple_auto_group_text("int y = 10;"))
        self.assertTrue(enhance_coverage.is_simple_auto_group_text("y += 2;"))
        self.assertFalse(enhance_coverage.is_simple_auto_group_text("if (x == 5)"))

    def test_strip_line_comment(self):
        self.assertEqual(enhance_coverage.strip_line_comment("int x = 5; // comment"), "int x = 5;")
        self.assertEqual(enhance_coverage.strip_line_comment("/* comment */ int y = 10;"), "/* comment */ int y = 10;")

    def test_normalize_code_for_hash(self):
        self.assertEqual(enhance_coverage.normalize_code_for_hash("  int   x   =   5;  "), "int x = 5;")

    def test_calc_text_hash(self):
        h = enhance_coverage.calc_text_hash("int x = 5;")
        self.assertEqual(len(h), 32)

    def test_calc_file_path_hash(self):
        h = enhance_coverage.calc_file_path_hash("src/main.c")
        self.assertEqual(len(h), 32)


class TestIntegration(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.input_dir = os.path.join(self.test_dir, "input")
        self.output_dir = os.path.join(self.test_dir, "output")
        os.makedirs(self.input_dir)
        os.makedirs(self.output_dir)

        # Create a mock gcov HTML file inside input_dir
        self.mock_html = """
        <!DOCTYPE html>
        <html>
        <head>
          <title>LCOV - cov - src/main.c</title>
        </head>
        <body>
          <pre class="source">
            <span class="lineNum"> 1 </span><span class="lineCov">  int main() {</span>
            <span class="lineNum"> 2 </span><span class="lineNoCov">    int a = 1;</span>
            <span class="lineNum"> 3 </span><span class="lineNoCov">    if (a == 1) {</span>
            <span class="lineNum"> 4 </span><span class="lineNoCov">      a = 2;</span>
            <span class="lineNum"> 5 </span><span class="lineNoCov">    }</span>
            <span class="lineNum"> 6 </span><span class="lineCov">    return 0;</span>
            <span class="lineNum"> 7 </span><span class="lineCov">  }</span>
          </pre>
        </body>
        </html>
        """
        with open(os.path.join(self.input_dir, "module.gcov.html"), "w", encoding="utf-8") as f:
            f.write(self.mock_html)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_extract_report_file_path(self):
        path = enhance_coverage.extract_report_file_path(self.mock_html, "fallback.gcov.html")
        self.assertEqual(path, "src/main.c")

    def test_extract_line_index_records(self):
        records = enhance_coverage.extract_line_index_records(self.mock_html, "fallback.gcov.html", "TestProj")
        self.assertTrue(len(records) > 0)
        self.assertEqual(records[0]["project_name"], "TestProj")
        self.assertEqual(records[0]["file_path"], "src/main.c")

    def test_write_configured_enhance_js(self):
        js_out = os.path.join(self.test_dir, "enhance_test.js")
        enhance_coverage.write_configured_enhance_js(js_out, "TestProjInject", "immediate")

        with open(js_out, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn('const DEFAULT_PROJECT = "TestProjInject";', content)
        self.assertIn('const RENDER_MODE = "immediate";', content)

    @unittest.mock.patch('enhance_coverage.DatabaseManager')
    def test_inject_coverage_report_lazy(self, mock_db_manager):
        # Configure DatabaseManager mock to avoid actual DB connections
        mock_db_manager.return_value = unittest.mock.MagicMock()

        enhance_coverage.inject_coverage_report(
            self.input_dir,
            self.output_dir,
            project_name="IntegrationLazyProj",
            workers=1,
            render_mode="lazy"
        )

        # Verify HTML injection and file copies
        enhanced_html = os.path.join(self.output_dir, "module.gcov.html")
        self.assertTrue(os.path.exists(enhanced_html))
        with open(enhanced_html, "r", encoding="utf-8") as f:
            html_content = f.read()

        self.assertIn("coverage_enhance.css?v=", html_content)
        self.assertIn("coverage_enhance.js?v=", html_content)

        # Verify JS values
        copied_js = os.path.join(self.output_dir, "coverage_enhance.js")
        self.assertTrue(os.path.exists(copied_js))
        with open(copied_js, "r", encoding="utf-8") as f:
            js_content = f.read()
        self.assertIn('const DEFAULT_PROJECT = "IntegrationLazyProj";', js_content)
        self.assertIn('const RENDER_MODE = "lazy";', js_content)

        # Verify CSS file
        copied_css = os.path.join(self.output_dir, "coverage_enhance.css")
        self.assertTrue(os.path.exists(copied_css))


class TestInheritAnalysis(unittest.TestCase):
    """Targeted testing for the cross-version analysis inheritance algorithm."""

    @unittest.mock.patch('enhance_coverage.db_module')
    def test_inherit_analysis_dict_rows(self, mock_db_module):
        # Setup mock database connection and cursor
        mock_db_module.__name__ = 'pymysql'
        mock_conn = unittest.mock.MagicMock()
        mock_cursor = unittest.mock.MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_db_module.connect.return_value = mock_conn

        # Mock counts returning values in order
        mock_cursor.fetchone.side_effect = [
            {"count": 10},  # source_analysis_records
            {"count": 5},   # source_reviewed_records
            {"count": 20},  # source_index_records
            {"count": 18},  # source_hashable_index_records
            {"count": 30},  # target_index_records
            {"count": 25},  # target_hashable_index_records
        ]

        # Mock fetchall returning dictionaries
        source_rows = [
            {
                "file_path": "src/main.c",
                "source_file_name": "main.c",
                "function_hash": "func_hash_1",
                "code_line_hash": "line_hash_1",
                "code_occurrence": 1,
                "reviewer": "Alice",
                "status": "已审核",
                "coverage_method": "Covered via UT",
                "uncovered_reason": ""
            }
        ]

        target_rows = [
            {
                "file_path": "src/main.c",
                "file_path_hash": "path_hash_1",
                "source_file_name": "main.c",
                "line_number": 42,
                "function_hash": "func_hash_1",
                "code_line_hash": "line_hash_1",
                "code_occurrence": 1,
                "status": "未确认",
                "coverage_method": "",
                "uncovered_reason": "",
                "reviewer": ""
            }
        ]

        mock_cursor.fetchall.side_effect = [
            source_rows,
            target_rows
        ]

        config = {
            "mysql": {
                "host": "127.0.0.1",
                "port": 3306,
                "user": "root",
                "password": "",
                "database": "coverage"
            }
        }

        manager = enhance_coverage.DatabaseManager(config, exit_on_error=False, init_schema=False)
        manager.conn = mock_conn

        # Execute inheritance
        result = manager.inherit_analysis("v1", "v2")

        # Verify results
        self.assertEqual(result["inherited_records"], 1)

        # Verify SQL execution args
        self.assertTrue(mock_cursor.executemany.called)
        exec_args = mock_cursor.executemany.call_args[0]
        self.assertIn("INSERT INTO coverage_analysis", exec_args[0])
        batch = exec_args[1]
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0][0], "v2")
        self.assertEqual(batch[0][1], "src/main.c")
        self.assertEqual(batch[0][5], "Alice")
        self.assertEqual(batch[0][6], "已审核")
        self.assertEqual(batch[0][8], "")

    @unittest.mock.patch('enhance_coverage.db_module')
    def test_inherit_analysis_tuple_rows(self, mock_db_module):
        # Setup mock database connection and cursor
        mock_db_module.__name__ = 'pymysql'
        mock_conn = unittest.mock.MagicMock()
        mock_cursor = unittest.mock.MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_db_module.connect.return_value = mock_conn

        # Mock counts returning values in order
        mock_cursor.fetchone.side_effect = [
            (10,),  # source_analysis_records
            (5,),   # source_reviewed_records
            (20,),  # source_index_records
            (18,),  # source_hashable_index_records
            (30,),  # target_index_records
            (25,),  # target_hashable_index_records
        ]

        # Mock fetchall returning tuples
        source_rows = [
            ("src/main.c", "main.c", "func_hash_1", "line_hash_1", 1, "Bob", "无法覆盖", "", "Hardware constraint")
        ]

        target_rows = [
            ("src/main.c", "path_hash_1", "main.c", 42, "func_hash_1", "line_hash_1", 1, "未确认", "", "", "")
        ]

        mock_cursor.fetchall.side_effect = [
            source_rows,
            target_rows
        ]

        config = {
            "mysql": {
                "host": "127.0.0.1",
                "port": 3306,
                "user": "root",
                "password": "",
                "database": "coverage"
            }
        }

        manager = enhance_coverage.DatabaseManager(config, exit_on_error=False, init_schema=False)
        manager.conn = mock_conn

        # Execute inheritance
        result = manager.inherit_analysis("v1", "v2")

        # Verify results
        self.assertEqual(result["inherited_records"], 1)

        # Verify SQL execution args
        self.assertTrue(mock_cursor.executemany.called)
        exec_args = mock_cursor.executemany.call_args[0]
        self.assertIn("INSERT INTO coverage_analysis", exec_args[0])
        batch = exec_args[1]
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0][0], "v2")
        self.assertEqual(batch[0][1], "src/main.c")
        self.assertEqual(batch[0][5], "Bob")
        self.assertEqual(batch[0][6], "无法覆盖")
        self.assertEqual(batch[0][8], "Hardware constraint")


class TestThreadLocalDatabase(unittest.TestCase):
    @unittest.mock.patch('enhance_coverage.db_module')
    def test_get_thread_db_manager(self, mock_db_module):
        # Mock connection and connect function
        mock_conn = unittest.mock.MagicMock()
        mock_db_module.connect.return_value = mock_conn
        mock_db_module.__name__ = 'pymysql'

        config = {
            "mysql": {
                "host": "127.0.0.1",
                "port": 3306,
                "user": "root",
                "password": "",
                "database": "coverage"
            }
        }

        # Retrieve connection inside main thread
        manager1 = enhance_coverage.get_thread_db_manager(config)
        manager2 = enhance_coverage.get_thread_db_manager(config)

        # Main thread calls should return the same instance (reused connection)
        self.assertIs(manager1, manager2)

        # Retrieve connection inside another thread
        import threading
        other_manager = []
        def thread_worker():
            m = enhance_coverage.get_thread_db_manager(config)
            other_manager.append(m)

        thread = threading.Thread(target=thread_worker)
        thread.start()
        thread.join()

        # Other thread must return a different instance (isolated context)
        self.assertIsNot(manager1, other_manager[0])

        # Clean up thread local manager
        enhance_coverage.close_thread_db_manager()


if __name__ == "__main__":
    unittest.main()
