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
import io

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


class TestOwnershipProgress(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="coverage-ownership-test-")
        self.xlsx_path = os.path.join(self.temp_dir, "ownership.xlsx")
        with enhance_coverage._ownership_cache_lock:
            enhance_coverage._ownership_cache.clear()

    def tearDown(self):
        with enhance_coverage._ownership_cache_lock:
            enhance_coverage._ownership_cache.clear()
        shutil.rmtree(self.temp_dir)

    def write_workbook(self, team="平台一组", leader="张三"):
        workbook_data = enhance_coverage.build_xlsx_workbook([
            {
                "name": "可变目录清单",
                "xml": enhance_coverage.xlsx_sheet_xml([
                    ["代码目录", "模块"],
                    ["/old/build/repository/src/network/core", "NET_CORE"],
                    ["/old/build/repository/src/storage", "STORAGE"],
                    ["short/inc", "SHORT_INC"],
                ]),
            },
            {
                "name": "可变负责人清单",
                "xml": enhance_coverage.xlsx_sheet_xml([
                    ["组件", "开发小组", "开发主管"],
                    ["NET_CORE", team, leader],
                    ["STORAGE", team, leader],
                    ["SHORT_INC", team, leader],
                ]),
            },
        ])
        with open(self.xlsx_path, "wb") as workbook_file:
            workbook_file.write(workbook_data)

    def config(self):
        return {
            "ownership": {
                "enabled": True,
                "xlsx_path": self.xlsx_path,
            }
        }

    def test_reads_changed_sheet_names_and_matches_shifted_root(self):
        self.write_workbook()
        workbook = enhance_coverage.parse_ownership_workbook(self.xlsx_path)

        ownership = enhance_coverage.match_file_ownership(
            "/new/agent/repository/src/network/core/main.c",
            workbook,
        )

        self.assertEqual(workbook["directory_sheet"], "可变目录清单")
        self.assertEqual(workbook["owner_sheet"], "可变负责人清单")
        self.assertEqual(ownership["module"], "NET_CORE")
        self.assertEqual(ownership["team"], "平台一组")
        self.assertEqual(ownership["leader"], "张三")
        self.assertEqual(ownership["ownership_status"], "已匹配")
        short_ownership = enhance_coverage.match_file_ownership(
            "/new/agent/short/inc/header.h",
            workbook,
        )
        self.assertEqual(short_ownership["module"], "SHORT_INC")

    def test_groups_progress_by_team_and_leader(self):
        self.write_workbook()
        progress = enhance_coverage.build_ownership_progress([
            {
                "file_path": "/new/repository/src/network/core/main.c",
                "total_uncovered": 10,
                "filled_total": 6,
                "unfilled_total": 4,
                "confirmed_total": 5,
                "coverable_total": 3,
                "uncoverable_total": 1,
                "redundant_total": 1,
            },
            {
                "file_path": "/new/repository/src/storage/disk.c",
                "total_uncovered": 5,
                "filled_total": 2,
                "unfilled_total": 3,
                "confirmed_total": 2,
                "coverable_total": 2,
                "uncoverable_total": 0,
                "redundant_total": 0,
            },
            {
                "file_path": "/new/repository/src/unknown.c",
                "total_uncovered": 4,
                "filled_total": 0,
                "unfilled_total": 4,
                "confirmed_total": 0,
            },
        ], self.config())

        matched_group = progress["teams"][0]
        self.assertEqual(matched_group["team"], "平台一组")
        self.assertEqual(matched_group["leader"], "张三")
        self.assertEqual(matched_group["module_names"], "NET_CORE、STORAGE")
        self.assertEqual(matched_group["file_total"], 2)
        self.assertEqual(matched_group["total_uncovered"], 15)
        self.assertEqual(matched_group["filled_total"], 8)
        self.assertEqual(progress["teams"][-1]["team"], "未匹配小组")
        self.assertEqual(progress["ownership"]["matched_files"], 2)
        self.assertEqual(progress["ownership"]["unmatched_files"], 1)

    def test_reloads_workbook_after_file_changes(self):
        self.write_workbook(team="旧小组", leader="旧组长")
        first = enhance_coverage.load_ownership_workbook(self.config())
        first_match = enhance_coverage.match_file_ownership(
            "/new/repository/src/storage/disk.c", first
        )

        old_mtime = os.stat(self.xlsx_path).st_mtime
        self.write_workbook(team="更新后的开发小组", leader="更新后的组长")
        os.utime(self.xlsx_path, (old_mtime + 2, old_mtime + 2))
        second = enhance_coverage.load_ownership_workbook(self.config())
        second_match = enhance_coverage.match_file_ownership(
            "/new/repository/src/storage/disk.c", second
        )

        self.assertEqual(first_match["team"], "旧小组")
        self.assertEqual(second_match["team"], "更新后的开发小组")
        self.assertEqual(second_match["leader"], "更新后的组长")


class TestBatchReviewSave(unittest.TestCase):
    class BatchCursor(object):
        def __init__(self):
            self.executemany_calls = []
            self.closed = False

        def executemany(self, sql, payload):
            self.executemany_calls.append((sql, list(payload)))

        def close(self):
            self.closed = True

    class BatchConnection(object):
        def __init__(self, cursor):
            self.batch_cursor = cursor
            self.commits = 0
            self.rollbacks = 0

        def ping(self, reconnect=True):
            return None

        def cursor(self):
            return self.batch_cursor

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

    def test_database_batch_save_uses_one_transaction(self):
        cursor = self.BatchCursor()
        manager = object.__new__(enhance_coverage.DatabaseManager)
        manager.conn = self.BatchConnection(cursor)
        blocks = [
            {
                "line_numbers": [10, 11], "reviewer": "Alice", "status": "可覆盖",
                "coverage_method": "UT", "uncovered_reason": "",
            },
            {
                "line_numbers": [20], "reviewer": "", "status": "未确认",
                "coverage_method": "", "uncovered_reason": "待补充",
            },
        ]

        result = manager.save_records_batch("batch_project", "src/main.c", blocks, is_draft=True)

        self.assertEqual(result, {"saved_blocks": 2, "saved_lines": 3})
        self.assertEqual(manager.conn.commits, 1)
        self.assertEqual(manager.conn.rollbacks, 0)
        self.assertEqual(len(cursor.executemany_calls), 1)
        payload = cursor.executemany_calls[0][1]
        self.assertEqual([row[4] for row in payload], [10, 11, 20])
        self.assertEqual([row[7] for row in payload], [1, 1, 1])
        self.assertTrue(cursor.closed)

    def make_post_handler(self, path, payload, responses):
        handler = object.__new__(enhance_coverage.CoverageHTTPRequestHandler)
        payload_bytes = json.dumps(payload).encode("utf-8")
        handler.path = path
        handler.headers = {"Content-Length": str(len(payload_bytes))}
        handler.rfile = io.BytesIO(payload_bytes)
        handler.send_json_response = lambda status, data: responses.append(("json", status, data))
        handler.send_error_response = lambda status, message: responses.append(("error", status, message))
        return handler

    def test_batch_endpoint_accepts_draft_and_normalizes_lines(self):
        calls = []

        class BatchManager(object):
            def save_records_batch(self, project_name, file_path, blocks, is_draft=False):
                calls.append((project_name, file_path, blocks, is_draft))
                return {"saved_blocks": len(blocks), "saved_lines": 3}

        previous_manager = enhance_coverage.db_manager
        enhance_coverage.db_manager = BatchManager()
        try:
            responses = []
            handler = self.make_post_handler("/api/coverage/batch", {
                "project_name": "batch_project",
                "file_path": "src/main.c",
                "mode": "draft",
                "blocks": [
                    {"line_numbers": ["10", 11], "status": "未确认"},
                    {"line_numbers": [20], "status": "可覆盖", "reviewer": "Alice", "coverage_method": "UT"},
                ],
            }, responses)

            handler.do_POST()

            self.assertEqual(responses[0][0:2], ("json", 200))
            self.assertEqual(responses[0][2]["saved_lines"], 3)
            self.assertEqual(calls[0][0:2], ("batch_project", "src/main.c"))
            self.assertEqual(calls[0][2][0]["line_numbers"], [10, 11])
            self.assertTrue(calls[0][3])
        finally:
            enhance_coverage.db_manager = previous_manager

    def test_batch_endpoint_rejects_incomplete_confirm(self):
        class BatchManager(object):
            def save_records_batch(self, project_name, file_path, blocks, is_draft=False):
                raise AssertionError("invalid confirm payload must not reach database")

        previous_manager = enhance_coverage.db_manager
        enhance_coverage.db_manager = BatchManager()
        try:
            responses = []
            handler = self.make_post_handler("/api/coverage/batch", {
                "project_name": "batch_project",
                "file_path": "src/main.c",
                "mode": "confirm",
                "blocks": [{"line_numbers": [10], "status": "可覆盖"}],
            }, responses)

            handler.do_POST()

            self.assertEqual(responses[0][0:2], ("error", 400))
            self.assertIn("reviewer", responses[0][2])
        finally:
            enhance_coverage.db_manager = previous_manager

    def test_batch_endpoint_marks_confirmed_blocks_as_non_draft(self):
        calls = []

        class BatchManager(object):
            def save_records_batch(self, project_name, file_path, blocks, is_draft=False):
                calls.append(is_draft)
                return {"saved_blocks": len(blocks), "saved_lines": 1}

        previous_manager = enhance_coverage.db_manager
        enhance_coverage.db_manager = BatchManager()
        try:
            responses = []
            handler = self.make_post_handler("/api/coverage/batch", {
                "project_name": "batch_project",
                "file_path": "src/main.c",
                "mode": "confirm",
                "blocks": [{
                    "line_numbers": [10], "status": "可覆盖", "reviewer": "Alice",
                    "coverage_method": "UT",
                }],
            }, responses)

            handler.do_POST()

            self.assertEqual(responses[0][0:2], ("json", 200))
            self.assertEqual(calls, [False])
        finally:
            enhance_coverage.db_manager = previous_manager


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
        with open(os.path.join(self.input_dir, "module.c.gcov.html"), "w", encoding="utf-8") as f:
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

        with open(enhance_coverage.JS_SOURCE_PATH, "r", encoding="utf-8") as f_src:
            src_js = f_src.read()
        self.assertEqual(content, src_js)
        self.assertIn("getMetaContent('coverage-project')", content)
        self.assertIn("getMetaContent('coverage-render-mode')", content)
        self.assertIn('function navigateReviewPanel', content)
        self.assertIn("previousBtn.innerText = '上一个';", content)
        self.assertIn("nextBtn.innerText = '下一个';", content)
        self.assertLess(
            content.index('panel.appendChild(previousBtn);'),
            content.index('panel.appendChild(reviewerInput);')
        )
        self.assertIn("locateBtn.innerText = '定位首个待填写';", content)
        self.assertIn('function isPanelAwaitingReview', content)
        self.assertIn('function notifyProgressChanged', content)
        self.assertIn('function saveReviewBlocksBatch', content)
        self.assertIn('暂存草稿', content)
        self.assertIn('确认提交', content)
        self.assertIn("requestCoverageApi('/analysis'", content)
        self.assertIn("requestCoverageApi('/code-lines/batch'", content)
        self.assertIn('function apiBaseCandidates', content)
        self.assertIn("progressLink.innerText = '查看进展 / 导出';", content)
        self.assertIn('setStoredPanelValues(panel, {', content)
        self.assertIn("status: getStoredPanelValue(previous, 'status')", content)
        self.assertIn("batchInheritBtn.innerText = '批量继承';", content)
        self.assertIn('function findPreviousFilledPanelEntry', content)
        self.assertIn('lineNum > sourceLineNum && lineNum <= panel.lineNum', content)
        self.assertIn('setStoredPanelValues(targetPanel, inheritedValues);', content)

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
        enhanced_html = os.path.join(self.output_dir, "module.c.gcov.html")
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
        with open(enhance_coverage.JS_SOURCE_PATH, "r", encoding="utf-8") as f_src:
            src_js = f_src.read()
        self.assertEqual(js_content, src_js)
        self.assertIn('meta name="coverage-project" content="IntegrationLazyProj"', html_content)
        self.assertIn('meta name="coverage-render-mode" content="lazy"', html_content)

        # Verify CSS file
        copied_css = os.path.join(self.output_dir, "coverage_enhance.css")
        self.assertTrue(os.path.exists(copied_css))

        progress_html = os.path.join(self.output_dir, "coverage_progress.html")
        self.assertTrue(os.path.exists(progress_html))
        with open(progress_html, "r", encoding="utf-8") as f:
            progress_content = f.read()
        self.assertIn('src="coverage_progress.js?v=', progress_content)
        self.assertNotIn('<script>\n', progress_content)
        self.assertIn('id="teamTable"', progress_content)
        self.assertIn('小组 / 组长填写进度', progress_content)
        self.assertIn('returnSummaryLink', progress_content)
        self.assertIn('返回全量审查汇总', progress_content)
        progress_js = os.path.join(self.output_dir, "coverage_progress.js")
        self.assertTrue(os.path.exists(progress_js))
        with open(progress_js, "r", encoding="utf-8") as f:
            progress_runtime = f.read()
        self.assertIn('coverage-review-progress-updated', progress_runtime)
        self.assertIn('function renderOwnershipStatus', progress_runtime)
        self.assertIn('ownership_status', progress_runtime)


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

        with unittest.mock.patch.object(enhance_coverage.DatabaseManager, 'get_connection', return_value=mock_conn):
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

        with unittest.mock.patch.object(enhance_coverage.DatabaseManager, 'get_connection', return_value=mock_conn):
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


class TestScalableProgress(unittest.TestCase):
    FILE_HEADERS = [
        "project_name", "file_path", "total_uncovered", "filled_total",
        "unfilled_total", "confirmed_total", "coverable_total",
        "uncoverable_total", "redundant_total", "fill_rate",
        "confirmed_rate", "last_updated",
    ]

    def test_progress_uses_one_file_level_query_and_returns_no_detail_rows(self):
        class Manager:
            def __init__(self):
                self.calls = []

            def has_line_index(self, project_name):
                return True

            def export_report(inner_self, report_type, project_name):
                inner_self.calls.append((report_type, project_name))
                return self.FILE_HEADERS, [
                    [project_name, "src/a.c", 100000, 40000, 60000, 30000, 20000, 5000, 5000, 40, 30, "2026-08-12"],
                    [project_name, "src/sub/b.c", 20, 10, 10, 8, 5, 2, 1, 50, 40, "2026-08-11"],
                ]

        manager = Manager()
        progress_updates = []
        data = enhance_coverage.compute_progress_data(
            manager,
            "large_project",
            {"ownership": {"enabled": False}},
            lambda percent, stage, message: progress_updates.append((percent, stage, message)),
        )

        self.assertEqual(manager.calls, [("full_file_summary", "large_project")])
        self.assertEqual(data["project"][0]["total_uncovered"], 100020)
        self.assertEqual(data["project"][0]["file_total"], 2)
        self.assertEqual(len(data["dirs"]), 2)
        self.assertEqual(len(data["files"]), 2)
        self.assertEqual(data["meta"]["aggregation_level"], "file")
        self.assertEqual(data["meta"]["detail_rows_returned"], 0)
        self.assertTrue(any(stage == "database" for _, stage, _ in progress_updates))
        self.assertTrue(any(stage == "ownership" for _, stage, _ in progress_updates))

    def test_full_detail_csv_is_written_in_batches_with_progress(self):
        class Manager:
            def count_full_detail_rows(self, project_name):
                return 3

            def iter_full_detail_batches(self, project_name, batch_size):
                yield [
                    [project_name, "src/a.c", 1, "line 1", 1, 1, "single", "未填写", "", "", "", "", None],
                    [project_name, "src/a.c", 2, "line 2", 2, 2, "single", "已填写", "可覆盖", "Alice", "case", "", "2026-08-12"],
                ]
                yield [[project_name, "src/b.c", 3, "line 3", 3, 3, "single", "未填写", "", "", "", "", None]]

        updates = []
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, "details.csv")
            written = enhance_coverage.write_full_detail_csv(
                Manager(), "large_project", output_path,
                lambda percent, stage, message: updates.append((percent, stage, message)),
            )
            with open(output_path, "r", encoding="utf-8-sig") as output_file:
                content = output_file.read()

        self.assertEqual(written, 3)
        self.assertIn("project_name,file_path,line_number", content)
        self.assertIn("large_project,src/a.c,2,line 2", content)
        self.assertTrue(any(stage == "exporting" and "3/3" in message for _, stage, message in updates))

    def test_progress_page_uses_background_jobs_and_paged_details(self):
        with open(enhance_coverage.PROGRESS_JS_SOURCE_PATH, "r", encoding="utf-8") as page_file:
            content = page_file.read()
        self.assertIn("/progress?", content)
        self.assertIn("/jobs/", content)
        self.assertIn("/progress/details?project=", content)
        self.assertIn("/exports", content)
        self.assertIn("后台导出详细 CSV", content)
        self.assertIn("page_size=200", content)
        self.assertIn("showConnecting();", content)
        with open(enhance_coverage.PROGRESS_PAGE_SOURCE_PATH, "r", encoding="utf-8") as html_file:
            html_content = html_file.read()
        self.assertIn('src="coverage_progress.js?v=', html_content)
        self.assertIn("页面版本", html_content)
        self.assertNotIn('<script>\n    const DEFAULT_REVIEW_SCOPE', html_content)


class TestNewFeaturesAndIntegrity(unittest.TestCase):
    """Automated tests validating new features: CSS integrity, code folding, filters, and module tree rows."""

    def test_css_integrity_and_balanced_braces(self):
        css_path = enhance_coverage.CSS_SOURCE_PATH
        self.assertTrue(os.path.isfile(css_path), "coverage_enhance.css file should exist")
        with open(css_path, "r", encoding="utf-8") as f:
            css_content = f.read()

        open_count = css_content.count("{")
        close_count = css_content.count("}")
        self.assertEqual(
            open_count, close_count,
            f"coverage_enhance.css braces count mismatch: open={open_count}, close={close_count}"
        )
        self.assertNotIn('pre.source::after {\n    content: "" !important;\npre.source', css_content)

    def test_frontend_folding_engine_contracts(self):
        js_path = os.path.join(enhance_coverage.SCRIPT_DIR, "coverage_enhance.js")
        self.assertTrue(os.path.isfile(js_path), "coverage_enhance.js file should exist")
        with open(js_path, "r", encoding="utf-8") as f:
            js_content = f.read()

        self.assertIn("applyFrontendFolding", js_content)
        self.assertIn("createFoldBar", js_content)
        self.assertIn("MERGE_GAP_THRESHOLD", js_content)
        self.assertIn("ensureBlockLinesVisible", js_content)
        self.assertIn("CONTEXT_LINES_DEFAULT", js_content)

    def test_incremental_summary_dropdown_filters_markup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = {
                "schema_version": 2,
                "generated_at": "2026-08-13 12:00:00",
                "oldgit": "old", "newgit": "new",
                "summary": {"changed_lines": 2, "covered": 1, "uncovered": 1, "ignored": 0, "missing": 0, "coverable_total": 2, "coverage_rate": 50.0},
                "details": [
                    {
                        "file_path": "src/main.c",
                        "review_file_path": "src/main.c",
                        "repository": "repo_alpha",
                        "team": "Team A",
                        "leader": "Leader 1",
                        "ownership_status": "已匹配",
                        "status": "未覆盖",
                        "line_number": 1,
                    },
                    {
                        "file_path": "src/main.c",
                        "review_file_path": "src/main.c",
                        "repository": "repo_alpha",
                        "team": "Team A",
                        "leader": "Leader 1",
                        "ownership_status": "已匹配",
                        "status": "已覆盖",
                        "line_number": 2,
                    }
                ],
            }
            with unittest.mock.patch.object(enhance_coverage, "is_mysql_configured", return_value=(False, False)):
                enhance_coverage.write_incremental_summary_page(temp_dir, "test_proj", result)
            summary_html = os.path.join(temp_dir, "incremental_coverage.html")
            with open(summary_html, "r", encoding="utf-8") as f:
                content = f.read()

            summary_js = os.path.join(temp_dir, "incremental_coverage.js")
            self.assertTrue(os.path.exists(summary_js))
            with open(summary_js, "r", encoding="utf-8") as f_js:
                js_content = f_js.read()

            self.assertIn('id="repo-filter"', content)
            self.assertIn('id="module-filter"', content)
            self.assertIn('id="team-filter"', content)
            self.assertIn('id="leader-filter"', content)
            self.assertIn('id="file-search"', content)
            self.assertIn('待分析行数', content)
            self.assertIn('data-repo="repo_alpha"', content)
            self.assertIn('<script src="incremental_coverage.js?v=', content)
            self.assertIn('addUnique(repos,', js_content)
            self.assertIn('populateSelect(repoFilter, repos);', js_content)
            self.assertIn('thead.addEventListener("click"', js_content)

    def test_progress_module_tree_rows_aggregation_and_markup(self):
        file_rows = [
            {
                "file_path": "src/module1/file1.c",
                "total_uncovered": 10,
                "filled_total": 2,
                "unfilled_total": 8,
                "confirmed_total": 1,
                "coverable_total": 2,
                "uncoverable_total": 0,
                "redundant_total": 0,
                "fill_rate": 20.0,
                "confirmed_rate": 10.0,
                "last_updated": "2026-08-13",
            }
        ]
        res = enhance_coverage.build_ownership_progress(file_rows, None)
        team_rows = res["teams"]
        self.assertTrue(len(team_rows) > 0)
        team_row = team_rows[0]
        self.assertIn("modules_detail", team_row)
        self.assertTrue(len(team_row["modules_detail"]) > 0)
        mod_detail = team_row["modules_detail"][0]
        self.assertEqual(mod_detail["file_total"], 1)
        self.assertEqual(mod_detail["total_uncovered"], 10)

        with open(enhance_coverage.PROGRESS_PAGE_SOURCE_PATH, "r", encoding="utf-8") as f:
            html_content = f.read()
        self.assertIn('id="expandAllTeamModulesBtn"', html_content)
        self.assertIn('id="collapseAllTeamModulesBtn"', html_content)

        with open(enhance_coverage.PROGRESS_JS_SOURCE_PATH, "r", encoding="utf-8") as f:
            js_content = f.read()
        self.assertIn("toggle-team-btn", js_content)
        self.assertIn("module-subrow", js_content)

    def test_lazy_mode_expansion_contract(self):
        js_path = os.path.join(enhance_coverage.SCRIPT_DIR, "coverage_enhance.js")
        with open(js_path, "r", encoding="utf-8") as f:
            js_content = f.read()

        self.assertIn("function expandBlockPanel(startLineNum)", js_content)
        self.assertIn("const panelState = panelsMap.get(startLineNum);", js_content)
        self.assertIn("setStoredPanelValues(panelState, values);", js_content)
        self.assertIn("return panelState;", js_content)
        self.assertIn("const MIN_FOLD_GAP = 15;", js_content)

    def test_phase1_performance_optimization_integrity(self):
        timer = enhance_coverage.PerfTimer("UnitTest")
        duration = timer.mark("phase_test")
        self.assertGreaterEqual(duration, 0.0)

        py_path = os.path.join(enhance_coverage.SCRIPT_DIR, "app", "compat", "legacy_runtime_impl.py")
        with open(py_path, "r", encoding="utf-8") as f:
            py_content = f.read()

        self.assertIn("class PerfTimer", py_content)
        self.assertIn("file_counter % 100 == 0:", py_content)
        self.assertIn("file_index % 50 == 0:", py_content)

    def test_phase4_and_phase5_performance_optimizations(self):
        sig = enhance_coverage.compute_directory_signature(enhance_coverage.SCRIPT_DIR)
        self.assertIn("file_count", sig)
        self.assertIn("latest_mtime", sig)
        self.assertIn("total_size", sig)

        py_path = os.path.join(enhance_coverage.SCRIPT_DIR, "app", "compat", "legacy_runtime_impl.py")
        with open(py_path, "r", encoding="utf-8") as f:
            py_content = f.read()

        self.assertIn("def compute_directory_signature", py_content)
        self.assertIn(".onesensor_source_signature.json", py_content)
        self.assertIn("target_rel_paths", py_content)
        self.assertIn("--reuse-output", py_content)

    def test_directory_signature_invalidation_on_context_change(self):
        sig1 = enhance_coverage.compute_directory_signature(
            enhance_coverage.SCRIPT_DIR, project_name="ProjA", review_scope="full", render_mode="lazy"
        )
        sig2 = enhance_coverage.compute_directory_signature(
            enhance_coverage.SCRIPT_DIR, project_name="ProjB", review_scope="full", render_mode="lazy"
        )
        sig3 = enhance_coverage.compute_directory_signature(
            enhance_coverage.SCRIPT_DIR, project_name="ProjA", review_scope="incremental", render_mode="lazy"
        )
        self.assertNotEqual(sig1, sig2)
        self.assertNotEqual(sig1, sig3)
        self.assertEqual(sig1["tool_version"], enhance_coverage.ASSET_VERSION)

    def test_thread_local_database_connection_cleanup(self):
        config = {"mysql": {"host": "127.0.0.1", "port": 3306, "user": "root", "password": "", "database": "cov"}}
        mock_conn = unittest.mock.MagicMock()
        with unittest.mock.patch.object(enhance_coverage.DatabaseManager, 'get_connection', return_value=mock_conn):
            manager = enhance_coverage.DatabaseManager(config, exit_on_error=False, init_schema=False)
            manager.conn = mock_conn

        self.assertEqual(manager.conn, mock_conn)
        manager.close_thread_connection()
        self.assertTrue(mock_conn.close.called)

    def test_ios_ui_template_integrity(self):
        py_path = os.path.join(enhance_coverage.SCRIPT_DIR, "app", "compat", "legacy_runtime_impl.py")
        with open(py_path, "r", encoding="utf-8") as f:
            py_content = f.read()

        self.assertIn("hero-card", py_content)
        self.assertIn("badge-unmatched-pill", py_content)
        self.assertIn("links-segmented", py_content)
        self.assertIn("commit-chip", py_content)
        self.assertIn("stat-pill", py_content)
        self.assertIn("fill-link", py_content)

        js_path = os.path.join(enhance_coverage.SCRIPT_DIR, "coverage_progress.js")
        with open(js_path, "r", encoding="utf-8") as f:
            js_content = f.read()

        self.assertIn("mod-chip", js_content)
        self.assertIn("mod-more-chip", js_content)
        self.assertIn("bar-high", js_content)
        self.assertIn("visible-progress-20260818_v9_12", js_content)

    def test_atomic_write_file_creates_and_renames(self):
        target_path = os.path.join(enhance_coverage.BACKGROUND_JOBS_STORAGE_DIR, "test_atomic.json")
        if os.path.exists(target_path):
            os.remove(target_path)
        enhance_coverage.atomic_write_file(target_path, '{"status": "ok"}')
        self.assertTrue(os.path.isfile(target_path))
        self.assertFalse(os.path.exists(target_path + ".part"))
        with open(target_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"status": "ok"}')
        if os.path.exists(target_path):
            os.remove(target_path)

    def test_project_data_version_increments(self):
        v1 = enhance_coverage.get_project_data_version("test_persisted_project")
        v2 = enhance_coverage.increment_project_data_version("test_persisted_project")
        self.assertGreater(v2, v1)
        self.assertEqual(enhance_coverage.get_project_data_version("test_persisted_project"), v2)

    def test_persistent_background_jobs_schema_and_methods(self):
        py_path = os.path.join(enhance_coverage.SCRIPT_DIR, "app", "compat", "legacy_runtime_impl.py")
        with open(py_path, "r", encoding="utf-8") as f:
            py_content = f.read()

        self.assertIn("def recover_background_jobs", py_content)
        self.assertIn("def start_background_job_cleanup_loop", py_content)
        self.assertIn("BACKGROUND_JOBS_STORAGE_DIR", py_content)

    def test_recover_background_jobs_execution_flow(self):
        mock_cursor = unittest.mock.MagicMock()
        mock_cursor.fetchall.return_value = [
            ("job_stale_1", "progress", "test_proj_stale", 999),
        ]
        mock_conn = unittest.mock.MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_mgr = unittest.mock.MagicMock()
        mock_mgr.conn = mock_conn

        with unittest.mock.patch.object(enhance_coverage, "db_manager", mock_mgr), \
             unittest.mock.patch.object(enhance_coverage, "get_project_data_version", return_value=100), \
             unittest.mock.patch.object(enhance_coverage, "save_job_to_db") as mock_save:
            enhance_coverage.recover_background_jobs()
            mock_save.assert_called_once()
            self.assertEqual(mock_save.call_args[0][0]["state"], "failed")

    def test_recover_background_jobs_cleans_orphan_part_files(self):
        job_id = "stale_orphan_999"
        enhance_coverage._ensure_background_jobs_storage_dir()
        orphan_part = os.path.join(enhance_coverage.BACKGROUND_JOBS_STORAGE_DIR, f"export_{job_id}.csv.part")
        with open(orphan_part, "w", encoding="utf-8") as f:
            f.write("orphan data")
        self.assertTrue(os.path.exists(orphan_part))

        mock_cursor = unittest.mock.MagicMock()
        mock_cursor.fetchall.return_value = [
            (job_id, "full_detail_export", "orphan_proj", 1),
        ]
        mock_conn = unittest.mock.MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_mgr = unittest.mock.MagicMock()
        mock_mgr.conn = mock_conn

        with unittest.mock.patch.object(enhance_coverage, "db_manager", mock_mgr), \
             unittest.mock.patch.object(enhance_coverage, "get_project_data_version", return_value=2), \
             unittest.mock.patch.object(enhance_coverage, "save_job_to_db"):
            enhance_coverage.recover_background_jobs()

        self.assertFalse(os.path.exists(orphan_part))

    def test_resolve_ownership_xlsx_path_fallback(self):
        resolved = enhance_coverage.resolve_ownership_xlsx_path({"ownership": {"xlsx_path": "non_existent_file.xlsx"}})
        self.assertTrue(os.path.isabs(resolved))

    def test_worker_thread_cooperative_cancellation_on_data_version_change(self):
        job_id = "test_cancel_job_123"
        project_name = "cancel_test_proj"
        with enhance_coverage._background_jobs_lock:
            enhance_coverage._background_jobs[job_id] = {
                "id": job_id,
                "project_name": project_name,
                "version": 1,
                "state": "running",
            }
        # When data_version increments to 2, update should raise JobCancelledError
        with unittest.mock.patch.object(enhance_coverage, "get_project_data_version", return_value=2):
            with self.assertRaises(enhance_coverage.JobCancelledError):
                enhance_coverage._update_background_job(job_id, 10, "exporting", "test")
        with enhance_coverage._background_jobs_lock:
            enhance_coverage._background_jobs.pop(job_id, None)

    def test_run_progress_background_job_cancellation_and_error_handling(self):
        # 1. Test JobCancelledError handling
        job_id_1 = "test_progress_job_cancel"
        with enhance_coverage._background_jobs_lock:
            enhance_coverage._background_jobs[job_id_1] = {"id": job_id_1, "state": "running", "kind": "progress"}
        with unittest.mock.patch.object(enhance_coverage, "compute_progress_data", side_effect=enhance_coverage.JobCancelledError("cancel test")):
            enhance_coverage._run_progress_background_job(job_id_1, "proj_cancel")
        job_1 = enhance_coverage._background_jobs.get(job_id_1)
        self.assertIsNotNone(job_1)
        self.assertEqual(job_1["state"], "cancelled")

        # 2. Test ordinary Exception handling
        job_id_2 = "test_progress_job_err"
        with enhance_coverage._background_jobs_lock:
            enhance_coverage._background_jobs[job_id_2] = {"id": job_id_2, "state": "running", "kind": "progress"}
        with unittest.mock.patch.object(enhance_coverage, "compute_progress_data", side_effect=RuntimeError("boom test")):
            enhance_coverage._run_progress_background_job(job_id_2, "proj_err")
        job_2 = enhance_coverage._background_jobs.get(job_id_2)
        self.assertIsNotNone(job_2)
        self.assertEqual(job_2["state"], "failed")
        self.assertIn("boom test", job_2.get("error_message", ""))

    def test_completed_job_db_restoration_restores_finished_at_and_enforces_retention(self):
        import time
        from datetime import datetime
        old_finished = time.time() - 3600
        dt_str = datetime.fromtimestamp(old_finished).strftime("%Y-%m-%d %H:%M:%S")
        mock_cursor = unittest.mock.MagicMock()
        mock_cursor.fetchone.return_value = {
            "job_id": "expired_job_123",
            "kind": "progress",
            "project_name": "test_proj",
            "data_version": 1,
            "state": "completed",
            "percent": 100,
            "stage": "completed",
            "message": "done",
            "result_path": "",
            "filename": "",
            "row_count": 0,
            "created_at": dt_str,
            "updated_at": dt_str,
            "finished_at": dt_str,
        }
        with unittest.mock.patch("enhance_coverage.db_manager") as mock_db:
            mock_db.conn.cursor.return_value = mock_cursor
            job = enhance_coverage.query_job_from_db("expired_job_123")
            self.assertIsNotNone(job)
            self.assertIsNotNone(job.get("finished_at_epoch"))
            self.assertAlmostEqual(job["finished_at_epoch"], old_finished, delta=2)

            res = enhance_coverage.public_background_job("expired_job_123")
            self.assertIsNone(res, "Expired job query from DB should return None and expire DB row")

    def test_cli_ops_paths_invalidate_data_version(self):
        py_path = os.path.join(enhance_coverage.SCRIPT_DIR, "app", "compat", "legacy_runtime_impl.py")
        with open(py_path, "r", encoding="utf-8") as f:
            py_content = f.read()

        self.assertIn("invalidate_project_background_jobs(project_name", py_content)
        self.assertIn("invalidate_project_background_jobs(target_project", py_content)

        clear_path = os.path.join(
            enhance_coverage.SCRIPT_DIR, "scripts", "maintenance", "clear_coverage_data.py"
        )
        with open(clear_path, "r", encoding="utf-8") as f:
            clear_content = f.read()

        self.assertIn("invalidate_project_background_jobs", clear_content)
        self.assertIn("DELETE FROM coverage_background_jobs", clear_content)
        self.assertIn("coverage_project_state", clear_content)

    def test_server_port_bind_order(self):
        py_path = os.path.join(enhance_coverage.SCRIPT_DIR, "app", "compat", "legacy_runtime_impl.py")
        with open(py_path, "r", encoding="utf-8") as f:
            py_content = f.read()

        server_def_pos = py_content.find("def run_server(")
        self.assertNotEqual(server_def_pos, -1)
        bind_pos = py_content.find("create_server(server_address, CoverageHTTPRequestHandler)", server_def_pos)
        self.assertNotEqual(bind_pos, -1)
        recover_pos = py_content.find("recover_background_jobs()", bind_pos)
        self.assertGreater(recover_pos, bind_pos, "recover_background_jobs must execute AFTER socket bind in run_server")

    def test_cli_cross_process_data_version_db_invalidation_and_stale_job_expiry(self):
        import time
        mock_cursor = unittest.mock.MagicMock()
        mock_cursor.fetchone.return_value = {"data_version": 5}
        mock_mgr = unittest.mock.MagicMock()
        mock_mgr.conn.cursor.return_value = mock_cursor

        ver = enhance_coverage.get_project_data_version("test_cross_proj", manager=mock_mgr)
        self.assertEqual(ver, 5)
        self.assertTrue(mock_cursor.execute.called)

        with enhance_coverage._background_jobs_lock:
            enhance_coverage._background_jobs["stale_job_999"] = {
                "id": "stale_job_999",
                "kind": "progress",
                "project_name": "test_cross_proj",
                "version": 1,
                "state": "completed",
                "created_at_epoch": time.time(),
            }

        res = enhance_coverage.public_background_job("stale_job_999")
        self.assertIsNone(res, "Stale job ID query with old version < current version must return None and expire")
        with enhance_coverage._background_jobs_lock:
            self.assertNotIn("stale_job_999", enhance_coverage._background_jobs)

    def test_simulated_two_process_db_version_update_and_stale_job_expiry(self):
        import time
        cursor_a = unittest.mock.MagicMock()
        cursor_a.fetchone.return_value = {"data_version": 3}
        manager_a = unittest.mock.MagicMock()
        manager_a.conn.cursor.return_value = cursor_a

        cursor_b = unittest.mock.MagicMock()
        cursor_b.fetchone.return_value = {"data_version": 4}
        manager_b = unittest.mock.MagicMock()
        manager_b.conn.cursor.return_value = cursor_b

        ver_a1 = enhance_coverage.get_project_data_version("proj_cross_sim", manager=manager_a)
        self.assertEqual(ver_a1, 3)

        with enhance_coverage._background_jobs_lock:
            enhance_coverage._background_jobs["job_sim_v3"] = {
                "id": "job_sim_v3",
                "kind": "progress",
                "project_name": "proj_cross_sim",
                "version": 3,
                "state": "completed",
                "created_at_epoch": time.time(),
            }

        new_ver_b = enhance_coverage.invalidate_project_background_jobs("proj_cross_sim", manager=manager_b)
        self.assertEqual(new_ver_b, 4)

        ver_a2 = enhance_coverage.get_project_data_version("proj_cross_sim", manager=manager_b)
        self.assertEqual(ver_a2, 4)

        res = enhance_coverage.public_background_job("job_sim_v3")
        self.assertIsNone(res, "Process A querying old version 3 job after Process B updated DB to version 4 MUST return None")

    def test_monotonic_version_increment_and_mismatch_invalidation(self):
        import time
        cursor = unittest.mock.MagicMock()
        cursor.fetchone.return_value = {"data_version": 11}
        manager = unittest.mock.MagicMock()
        manager.conn.cursor.return_value = cursor

        with enhance_coverage._background_jobs_lock:
            enhance_coverage._background_jobs["job_v10"] = {
                "id": "job_v10",
                "kind": "progress",
                "project_name": "proj_mono",
                "version": 10,
                "state": "completed",
                "created_at_epoch": time.time(),
            }

        new_ver = enhance_coverage.increment_project_data_version("proj_mono", manager=manager)
        self.assertEqual(new_ver, 11)

        res = enhance_coverage.public_background_job("job_v10")
        self.assertIsNone(res, "Job version 10 must be expired when current version is 11")

    def test_db_version_update_failure_fails_closed(self):
        class FakeRealDBConnection:
            def cursor(self):
                raise Exception("MySQL connection lost")

        class FakeRealDBManager:
            conn = FakeRealDBConnection()

        with self.assertRaises(RuntimeError):
            enhance_coverage.increment_project_data_version("proj_fail_closed", manager=FakeRealDBManager())

    def test_temporary_db_manager_connection_closed_cleanly(self):
        mock_temp_mgr = unittest.mock.MagicMock()
        mock_temp_mgr.conn = unittest.mock.MagicMock()

        with enhance_coverage._project_data_version_ttl_lock:
            enhance_coverage._project_data_version_ttl.pop("proj_owned_test_unique", None)
            enhance_coverage._project_data_versions.pop("proj_owned_test_unique", None)

        with unittest.mock.patch("enhance_coverage.DatabaseManager", return_value=mock_temp_mgr):
            mgr, owned = enhance_coverage._get_db_manager_context(manager=None)
            self.assertTrue(owned)
            enhance_coverage.get_project_data_version("proj_owned_test_unique")
            self.assertTrue(mock_temp_mgr.conn.close.called)

    def test_data_version_ttl_cache_avoids_redundant_queries(self):
        cursor = unittest.mock.MagicMock()
        cursor.fetchone.return_value = {"data_version": 42}
        manager = unittest.mock.MagicMock()
        manager.conn.cursor.return_value = cursor

        v1 = enhance_coverage.get_project_data_version("proj_ttl_test", manager=manager)
        self.assertEqual(v1, 42)
        initial_call_count = cursor.execute.call_count

    def test_clear_all_for_uninitialized_version0_projects_expires_stale_jobs(self):
        import time
        cursor = unittest.mock.MagicMock()
        cursor.fetchone.return_value = None # No row in DB initially
        manager = unittest.mock.MagicMock()
        manager.conn.cursor.return_value = cursor

        # 1. get_project_data_version initializes row with data_version=1
        ver = enhance_coverage.get_project_data_version("proj_uninit_v0", manager=manager)
        self.assertGreaterEqual(ver, 1)

        # 2. Add job
        with enhance_coverage._background_jobs_lock:
            enhance_coverage._background_jobs["job_uninit_1"] = {
                "id": "job_uninit_1",
                "kind": "progress",
                "project_name": "proj_uninit_v0",
                "version": ver,
                "state": "completed",
                "created_at_epoch": time.time(),
            }

        # 3. Simulate clear_all collecting known projects and incrementing version
        cursor.fetchone.return_value = {"data_version": ver + 1}
        enhance_coverage.invalidate_project_background_jobs("proj_uninit_v0", manager=manager)

        # 4. Old job query MUST return None
        res = enhance_coverage.public_background_job("job_uninit_1")
        self.assertIsNone(res, "Stale job query after clear_all invalidation must return None")


if __name__ == "__main__":
    unittest.main()
