"""
Unit tests for source_reader.py and code_detail_service.py.
"""

import os
import tempfile
import unittest
from code_region import FunctionRange
from code_detail_service import CodeDetailService, is_safe_relative_path
from source_reader import (
    SourceContext,
    SourceLineDTO,
    parse_source_lines_from_gcov_html,
    read_source_lines,
    read_source_ranges,
)


class MockDatabaseManager:
    """Mock database manager for testing CodeDetailService."""

    def __init__(self, records=None):
        self.records = records or []

    def fetch_records(self, project_name, file_path):
        return self.records


class TestSourceReaderAndService(unittest.TestCase):

    def setUp(self):
        self.mock_legacy_html = """
        <!DOCTYPE html>
        <html>
        <head><title>LCOV - cov - src/calculator.c</title></head>
        <body>
          <pre class="source">
            <span class="lineNum"> 1 </span><span class="lineCov">  #include &lt;stdio.h&gt;</span>
            <span class="lineNum"> 2 </span><span class="lineCov">  </span>
            <span class="lineNum"> 3 </span><span class="lineCov">  int add(int a, int b) {</span>
            <span class="lineNum"> 4 </span><span class="lineCov">      return a + b;</span>
            <span class="lineNum"> 5 </span><span class="lineCov">  }</span>
            <span class="lineNum"> 6 </span><span class="lineCov">  </span>
            <span class="lineNum"> 7 </span><span class="lineCov">  int divide(int a, int b) {</span>
            <span class="lineNum"> 8 </span><span class="lineNoCov">      if (b == 0) {</span>
            <span class="lineNum"> 9 </span><span class="lineNoCov">          return -1;</span>
            <span class="lineNum"> 10 </span><span class="lineCov">      }</span>
            <span class="lineNum"> 11 </span><span class="lineNoCov">      return a / b;</span>
            <span class="lineNum"> 12 </span><span class="lineCov">  }</span>
            <span class="lineNum"> 13 </span><span class="lineCov">  </span>
            <span class="lineNum"> 14 </span><span class="lineCov">  int main() {</span>
            <span class="lineNum"> 15 </span><span class="lineCov">      return add(1, 2);</span>
            <span class="lineNum"> 16 </span><span class="lineCov">  }</span>
          </pre>
        </body>
        </html>
        """

        self.mock_modern_html = """
        <!DOCTYPE html>
        <html>
        <head><title>LCOV - cov - src/modern.c</title></head>
        <body>
          <pre class="source">
            <span class="lineNum"> 1 </span><span id="L1" class="tlaGNC">int foo(int x) {</span>
            <span class="lineNum"> 2 </span><span id="L2" class="tlaUNC">    if (x &gt; 0) {</span>
            <span class="lineNum"> 3 </span><span id="L3" class="tlaUNC">        return x * 2;</span>
            <span class="lineNum"> 4 </span><span id="L4" class="tlaGNC">    }</span>
            <span class="lineNum"> 5 </span><span id="L5" class="tlaGNC">    return 0;</span>
            <span class="lineNum"> 6 </span><span id="L6" class="tlaGNC">}</span>
          </pre>
        </body>
        </html>
        """

    def test_parse_legacy_gcov_html(self):
        ctx = parse_source_lines_from_gcov_html(
            self.mock_legacy_html,
            project_name="TestProj",
            file_path="src/calculator.c",
        )
        self.assertEqual(ctx.total_lines, 16)
        self.assertEqual(ctx.file_path, "src/calculator.c")
        self.assertEqual(len(ctx.lines), 16)

        # Lines 8, 9, 11 are uncovered
        self.assertEqual(ctx.pending_lines, [8, 9, 11])

        # Check line DTO structure
        line8 = ctx.get_line(8)
        self.assertIsNotNone(line8)
        self.assertEqual(line8.line_no, 8)
        self.assertEqual(line8.coverage_state, "uncovered")
        self.assertTrue(line8.is_pending_analysis)
        self.assertEqual(line8.function_name, "divide")
        self.assertEqual(line8.block_type, "control_flow")

        # Functions detected: add (3..5), divide (7..12), main (14..16)
        self.assertTrue(len(ctx.function_ranges) >= 3)
        fn_names = [f.name for f in ctx.function_ranges]
        self.assertIn("add", fn_names)
        self.assertIn("divide", fn_names)
        self.assertIn("main", fn_names)

    def test_parse_modern_gcov_html(self):
        ctx = parse_source_lines_from_gcov_html(
            self.mock_modern_html,
            project_name="TestModern",
            file_path="src/modern.c",
        )
        self.assertEqual(ctx.total_lines, 6)
        self.assertEqual(ctx.pending_lines, [2, 3])

        line2 = ctx.get_line(2)
        self.assertEqual(line2.coverage_state, "uncovered")
        self.assertTrue(line2.is_pending_analysis)

        line5 = ctx.get_line(5)
        self.assertEqual(line5.coverage_state, "covered")
        self.assertFalse(line5.is_pending_analysis)

    def test_parse_with_analysis_records(self):
        records = [
            {
                "line_number": 8,
                "status": "可覆盖",
                "reviewer": "Alice",
                "coverage_method": "Unit test branch",
                "uncovered_reason": "",
                "is_draft": False,
            },
            {
                "line_number": 9,
                "status": "无法覆盖",
                "reviewer": "Bob",
                "coverage_method": "",
                "uncovered_reason": "Defensive check",
                "is_draft": False,
            },
            {
                "line_number": 11,
                "status": "未确认",
                "reviewer": "",
                "coverage_method": "",
                "uncovered_reason": "",
                "is_draft": True,
            },
        ]
        ctx = parse_source_lines_from_gcov_html(
            self.mock_legacy_html,
            project_name="TestProj",
            file_path="src/calculator.c",
            analysis_records=records,
        )

        # Line 8 & 9 are confirmed -> not pending
        # Line 11 is draft / 未确认 -> pending
        self.assertEqual(ctx.pending_lines, [11])

        line8 = ctx.get_line(8)
        self.assertEqual(line8.analysis_state, "可覆盖")
        self.assertEqual(line8.reviewer, "Alice")
        self.assertFalse(line8.is_pending_analysis)

        line11 = ctx.get_line(11)
        self.assertEqual(line11.analysis_state, "未确认")
        self.assertTrue(line11.is_draft)
        self.assertTrue(line11.is_pending_analysis)

    def test_read_source_lines(self):
        ctx = parse_source_lines_from_gcov_html(
            self.mock_legacy_html, project_name="Test", file_path="calc.c"
        )
        lines = read_source_lines(ctx, 3, 5)
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["line_no"], 3)
        self.assertEqual(lines[1]["line_no"], 4)
        self.assertEqual(lines[2]["line_no"], 5)

        # Single line start == end
        single = read_source_lines(ctx, 8, 8)
        self.assertEqual(len(single), 1)
        self.assertEqual(single[0]["line_no"], 8)

        # Full range 1..16
        full = read_source_lines(ctx, 1, 16)
        self.assertEqual(len(full), 16)

        # Invalid bounds
        with self.assertRaises(ValueError):
            read_source_lines(ctx, 0, 5)
        with self.assertRaises(ValueError):
            read_source_lines(ctx, 5, 2)
        with self.assertRaises(ValueError):
            read_source_lines(ctx, 1, 100)

    def test_read_source_ranges(self):
        ctx = parse_source_lines_from_gcov_html(
            self.mock_legacy_html, project_name="Test", file_path="calc.c"
        )
        ranges = [
            {"start_line": 3, "end_line": 5},
            {"start_line": 7, "end_line": 12},
        ]
        result = read_source_ranges(ctx, ranges)
        self.assertEqual(len(result), 2)
        self.assertEqual(len(result[0]["lines"]), 3)
        self.assertEqual(len(result[1]["lines"]), 6)

        # Over 100 ranges limit
        excessive = [{"start_line": 1, "end_line": 1} for _ in range(101)]
        with self.assertRaises(ValueError):
            read_source_ranges(ctx, excessive, max_ranges=100)

    def test_safe_relative_path(self):
        self.assertTrue(is_safe_relative_path("src/main.c"))
        self.assertTrue(is_safe_relative_path("module/sub/test.c.gcov.html"))
        self.assertFalse(is_safe_relative_path("/etc/passwd"))
        self.assertFalse(is_safe_relative_path("../secret.c"))
        self.assertFalse(is_safe_relative_path("src/../../etc/shadow"))
        self.assertFalse(is_safe_relative_path("C:\\Windows\\win.ini"))
        self.assertFalse(is_safe_relative_path(""))

    def test_code_detail_service_layout_and_batch(self):
        service = CodeDetailService(db_manager=None)

        # Layout computation using content override
        layout = service.get_code_layout(
            project_name="TestProj",
            file_path="src/calculator.c",
            content_override=self.mock_legacy_html,
        )

        self.assertEqual(layout["project_name"], "TestProj")
        self.assertEqual(layout["file_path"], "src/calculator.c")
        self.assertEqual(layout["total_lines"], 16)
        self.assertEqual(layout["pending_line_count"], 3)  # lines 8, 9, 11
        self.assertTrue(len(layout["regions"]) > 0)
        self.assertIn("perf", layout)
        self.assertIn("layout_build_ms", layout["perf"])

        # divide function (7..13) should be expanded
        expanded_regions = [r for r in layout["regions"] if r["default_state"] == "expanded"]
        self.assertEqual(len(expanded_regions), 1)
        self.assertEqual(expanded_regions[0]["start_line"], 7)
        self.assertEqual(expanded_regions[0]["end_line"], 13)
        self.assertEqual(expanded_regions[0]["label"], "divide")

        # Batch lines loading
        batch = service.get_code_lines_batch(
            project_name="TestProj",
            file_path="src/calculator.c",
            ranges=[{"start_line": 7, "end_line": 13}],
            content_override=self.mock_legacy_html,
        )
        self.assertEqual(len(batch["ranges"]), 1)
        self.assertEqual(len(batch["ranges"][0]["lines"]), 7)
        self.assertEqual(batch["ranges"][0]["lines"][0]["line_no"], 7)
        self.assertEqual(batch["ranges"][0]["lines"][-1]["line_no"], 13)
        self.assertIn("batch_load_ms", batch["perf"])

        # Single range loading
        single = service.get_code_lines_single(
            project_name="TestProj",
            file_path="src/calculator.c",
            start_line=1,
            end_line=5,
            content_override=self.mock_legacy_html,
        )
        self.assertEqual(len(single["lines"]), 5)
        self.assertEqual(single["start_line"], 1)
        self.assertEqual(single["end_line"], 5)

    def test_service_with_gcov_file_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_on_disk = os.path.join(tmp_dir, "test_file.c.gcov.html")
            with open(file_on_disk, "w", encoding="utf-8") as f:
                f.write(self.mock_legacy_html)

            service = CodeDetailService(search_dirs=[tmp_dir])
            layout = service.get_code_layout("TestProj", "test_file.c")
            self.assertEqual(layout["total_lines"], 16)
            self.assertEqual(layout["pending_line_count"], 3)


if __name__ == "__main__":
    unittest.main()
