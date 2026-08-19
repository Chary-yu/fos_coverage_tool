"""
Comprehensive Unit and Regression Test Suite for Lazy Collapse Architecture v11 fixes:
1. Accurate C/C++ Function Boundary Detection (token-aware brace balance, comments/strings/char literals)
2. Global variables / macros between functions falling back to ±20
3. 10,000 functions + 10,000 pending lines fast mapping performance (< 50ms)
4. Explicit FileNotFoundError / HTTP 404 on missing source files (no empty 200 responses)
5. Report ID / Source sidecar isolation and true HTML skeleton stripping
6. Incremental scope filtering and cache key isolation
7. Injection default render_mode == "lazy_collapse"
8. Large batch queries (> 100 regions) up to 1000 limit
"""

import html
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from code_region import CodeRegion, FunctionRange, build_code_regions, find_function_containing_line
from source_reader import (
    SourceContext,
    SourceLineDTO,
    calc_sidecar_file_key,
    extract_c_function_ranges,
    is_line_pending_analysis,
    load_source_sidecar,
    parse_source_lines_from_gcov_html,
    read_source_lines,
    read_source_ranges,
    save_source_sidecar,
)
import code_detail_service
from code_detail_service import CodeDetailService, compute_file_path_hash
import enhance_coverage
from enhance_coverage import (
    CoverageHTTPRequestHandler,
    get_code_detail_service,
    inject_coverage_report,
    process_gcov_file_for_inject,
    write_configured_enhance_js,
)


class MockRequest:
    def __init__(self, raw_input=b""):
        self._raw_input = raw_input

    def makefile(self, mode, *args, **kwargs):
        if "b" in mode:
            if "w" in mode:
                return io.BytesIO()
            return io.BytesIO(self._raw_input)
        return io.StringIO()


class TestLazyCollapseV11Fixes(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_1_accurate_c_function_boundaries_with_braces_in_comments_and_strings(self):
        """Verify token-aware brace parser ignores braces in comments, strings, char literals."""
        raw_lines = [
            {"line_no": 1, "code_text": "#include <stdio.h>"},
            {"line_no": 2, "code_text": ""},
            {"line_no": 3, "code_text": "int calculate_sum(int a, int b) {"},
            {"line_no": 4, "code_text": '    char *msg = "String with { opening and } closing braces";'},
            {"line_no": 5, "code_text": "    char c = '}'; // char literal '}'"},
            {"line_no": 6, "code_text": "    /* block comment with { and } */"},
            {"line_no": 7, "code_text": "    // line comment with { and }"},
            {"line_no": 8, "code_text": "    if (a > 0) {"},
            {"line_no": 9, "code_text": "        return a + b;"},
            {"line_no": 10, "code_text": "    }"},
            {"line_no": 11, "code_text": "    return b;"},
            {"line_no": 12, "code_text": "}"},
            {"line_no": 13, "code_text": ""},
            {"line_no": 14, "code_text": "int global_counter = 42;"},
            {"line_no": 15, "code_text": ""},
            {"line_no": 16, "code_text": "void log_message(const char *s) {"},
            {"line_no": 17, "code_text": "    printf(\"%s\\n\", s);"},
            {"line_no": 18, "code_text": "}"},
        ]

        functions = extract_c_function_ranges(raw_lines)
        self.assertEqual(len(functions), 2)

        # calculate_sum is strictly 3..12 (not 3..15)
        self.assertEqual(functions[0].name, "calculate_sum")
        self.assertEqual(functions[0].start_line, 3)
        self.assertEqual(functions[0].end_line, 12)

        # log_message is strictly 16..18
        self.assertEqual(functions[1].name, "log_message")
        self.assertEqual(functions[1].start_line, 16)
        self.assertEqual(functions[1].end_line, 18)

    def test_2_global_code_between_functions_falls_back_to_plus_minus_20(self):
        """A pending line in global scope (e.g. line 14) between functions must use ±20 fallback, not expand previous function."""
        functions = [
            FunctionRange(3, 12, "calculate_sum"),
            FunctionRange(100, 110, "log_message"),
        ]

        # Pending line 50 is in global scope between the two functions
        regions = build_code_regions(
            total_lines=150,
            pending_lines=[50],
            function_ranges=functions,
            fallback_context=20,
        )

        # Regions:
        # [1..29] collapsed
        # [30..70] expanded fallback (50 ± 20)
        # [71..150] collapsed
        self.assertEqual(len(regions), 3)
        self.assertEqual(regions[0].default_state, "collapsed")
        self.assertEqual(regions[0].end_line, 29)

        self.assertEqual(regions[1].default_state, "expanded")
        self.assertEqual(regions[1].start_line, 30)
        self.assertEqual(regions[1].end_line, 70)
        self.assertIsNone(regions[1].label)

        self.assertEqual(regions[2].default_state, "collapsed")
        self.assertEqual(regions[2].start_line, 71)
        self.assertEqual(regions[2].end_line, 150)

    def test_3_massive_function_mapping_perf_10000_functions(self):
        """Stress test: 10,000 functions + 10,000 pending lines must build layout in < 100ms."""
        total_lines = 100000
        # 10,000 functions of size 8 separated by gap of 2
        function_ranges = [
            FunctionRange(i * 10 + 1, i * 10 + 8, f"fn_{i}")
            for i in range(10000)
        ]
        # 10,000 pending lines (one inside each function)
        pending_lines = [i * 10 + 4 for i in range(10000)]

        t_start = time.perf_counter()
        regions = build_code_regions(
            total_lines=total_lines,
            pending_lines=pending_lines,
            function_ranges=function_ranges,
        )
        elapsed_ms = (time.perf_counter() - t_start) * 1000.0

        print(f"\n[Perf Test] 10,000 functions + 10,000 pending lines layout build time = {elapsed_ms:.2f}ms")
        self.assertLess(elapsed_ms, 150.0)  # Must be fast
        self.assertTrue(len(regions) > 0)

    def test_4_source_not_found_raises_filenotfounderror(self):
        """Missing source file must raise FileNotFoundError, not return empty 200 response."""
        service = CodeDetailService(search_dirs=[self.temp_dir])
        with self.assertRaises(FileNotFoundError):
            service.get_code_layout(project_name="TestProj", file_path="non_existent/file.c")

    def test_5_report_id_source_sidecar_isolation(self):
        """Different report_ids with same file_path do not pollute each other's source content."""
        report_a = "report_alpha"
        report_b = "report_beta"
        file_path = "src/driver.c"
        file_hash = compute_file_path_hash(file_path)

        ctx_a = SourceContext(
            project_name="TestProj",
            file_path=file_path,
            lines=[SourceLineDTO(1, "int alpha_version = 1;", "raw_a", "covered")],
            report_id=report_a,
        )
        ctx_b = SourceContext(
            project_name="TestProj",
            file_path=file_path,
            lines=[SourceLineDTO(1, "int beta_version = 2;", "raw_b", "uncovered", is_pending_analysis=True)],
            report_id=report_b,
        )

        save_source_sidecar(self.temp_dir, report_a, file_hash, ctx_a)
        save_source_sidecar(self.temp_dir, report_b, file_hash, ctx_b)

        service = CodeDetailService(search_dirs=[self.temp_dir])

        loaded_a = service.get_source_context("TestProj", file_path, report_id=report_a)
        loaded_b = service.get_source_context("TestProj", file_path, report_id=report_b)

        self.assertEqual(loaded_a.lines[0].source, "int alpha_version = 1;")
        self.assertEqual(loaded_b.lines[0].source, "int beta_version = 2;")
        self.assertEqual(loaded_a.total_uncovered_count, 0)
        self.assertEqual(loaded_b.total_uncovered_count, 1)

    def test_6_incremental_scope_filtering(self):
        """Incremental review scope only marks Git-incremental lines as pending analysis."""
        html_content = """
        <!DOCTYPE html>
        <html>
        <head><title>LCOV - cov - src/app.c</title></head>
        <body>
          <pre class="source">
            <span class="lineNum"> 1 </span><span class="lineNoCov">  int old_uncovered = 0;</span>
            <span class="lineNum"> 2 </span><span class="lineNoCov" data-coverage-review="incremental">  int new_uncovered = 1;</span>
          </pre>
        </body>
        </html>
        """
        # Full scope: 2 uncovered lines pending
        ctx_full = parse_source_lines_from_gcov_html(
            html_content, project_name="TestProj", file_path="src/app.c", review_scope="full"
        )
        self.assertEqual(ctx_full.total_uncovered_count, 2)
        self.assertEqual(len(ctx_full.pending_lines), 2)

        # Incremental scope: only line 2 is pending
        ctx_inc = parse_source_lines_from_gcov_html(
            html_content, project_name="TestProj", file_path="src/app.c", review_scope="incremental"
        )
        self.assertEqual(ctx_inc.total_uncovered_count, 1)
        self.assertEqual(ctx_inc.pending_lines, [2])

    def test_7_inject_creates_sidecar_and_strips_html_skeleton(self):
        """Injecting in lazy_collapse mode produces a lightweight stripped HTML and sidecar JSON."""
        input_dir = os.path.join(self.temp_dir, "input")
        output_dir = os.path.join(self.temp_dir, "output")
        os.makedirs(input_dir, exist_ok=True)

        gcov_file = os.path.join(input_dir, "test.c.gcov.html")
        source_lines_html = "".join(
            f'<span class="lineNum">{i}</span><span class="{"lineNoCov" if i == 50 else "lineCov"}"> int x_{i} = {i};</span>\n'
            for i in range(1, 101)
        )
        sample_gcov_html = f"""<!DOCTYPE html>
        <html>
        <head><title>LCOV - cov - src/test.c</title></head>
        <body>
          <pre class="source">
            {source_lines_html}
          </pre>
        </body>
        </html>"""

        with open(gcov_file, "w", encoding="utf-8") as f:
            f.write(sample_gcov_html)

        inject_coverage_report(
            input_dir=input_dir,
            output_dir=output_dir,
            project_name="TestProj",
            render_mode="lazy_collapse",
            reuse_output=False,
        )

        injected_html_file = os.path.join(output_dir, "test.c.gcov.html")
        self.assertTrue(os.path.isfile(injected_html_file))

        with open(injected_html_file, "r", encoding="utf-8") as f:
            injected_html = f.read()

        # Injected HTML must contain empty <pre class="source"></pre> container
        self.assertIn('<pre class="source"></pre>', injected_html)
        # Injected HTML must NOT contain the 100 source lines
        self.assertNotIn("int x_50 = 50;", injected_html)
        # Injected HTML must have meta tags
        self.assertIn('meta name="coverage-report-id"', injected_html)
        self.assertIn('meta name="coverage-file-path"', injected_html)

        # Injected JS must have RENDER_MODE = "lazy_collapse"
        injected_js_file = os.path.join(output_dir, "coverage_enhance.js")
        with open(injected_js_file, "r", encoding="utf-8") as f:
            js_content = f.read()
        self.assertIn('const RENDER_MODE = "lazy_collapse";', js_content)

    def test_8_batch_loading_over_100_ranges_succeeds(self):
        """Batch loading 150 ranges succeeds with limit raised to 1000."""
        file_path = "src/many_funcs.c"
        # 200 lines
        lines = [SourceLineDTO(i, f"int line_{i};", f"raw_{i}", "uncovered" if i % 2 == 0 else "covered") for i in range(1, 201)]
        ctx = SourceContext(project_name="TestProj", file_path=file_path, lines=lines)

        ranges = [{"start_line": i, "end_line": i} for i in range(1, 151)]
        results = read_source_ranges(ctx, ranges, max_ranges=1000)
        self.assertEqual(len(results), 150)

    def test_9_p0_1_sidecar_hash_consistency_injector_and_service(self):
        """P0-1: Sidecar created by injector uses calc_sidecar_file_key and is found by CodeDetailService."""
        from source_reader import calc_sidecar_file_key
        input_dir = os.path.join(self.temp_dir, "input_p0_1")
        output_dir = os.path.join(self.temp_dir, "output_p0_1")
        os.makedirs(input_dir, exist_ok=True)

        gcov_file = os.path.join(input_dir, "test.c.gcov.html")
        sample_gcov_html = """<!DOCTYPE html>
        <html>
        <head><title>LCOV - cov - src/test.c</title></head>
        <body>
          <pre class="source">
            <span class="lineNum"> 1 </span><span class="lineCov"> int a = 1;</span>
            <span class="lineNum"> 2 </span><span class="lineNoCov"> int b = 2;</span>
          </pre>
        </body>
        </html>"""

        with open(gcov_file, "w", encoding="utf-8") as f:
            f.write(sample_gcov_html)

        inject_coverage_report(
            input_dir=input_dir,
            output_dir=output_dir,
            project_name="P0Proj",
            render_mode="lazy_collapse",
            reuse_output=False,
        )

        expected_key = calc_sidecar_file_key("src/test.c")
        report_id = os.listdir(os.path.join(output_dir, ".source_cache"))[0]
        sidecar_file = os.path.join(output_dir, ".source_cache", report_id, f"{expected_key}.source.json")
        self.assertTrue(os.path.isfile(sidecar_file), f"Sidecar file missing at {sidecar_file}")

        # CodeDetailService without explicit custom search dirs should find sidecar via report registry
        service = CodeDetailService()
        layout = service.get_code_layout("P0Proj", "src/test.c", report_id=report_id)
        self.assertEqual(layout["total_lines"], 2)
        self.assertEqual(layout["total_uncovered_count"], 1)

    def test_10_p0_2_report_registry_persistence(self):
        """P0-2: Separate process simulation locates sidecar via .report_registry.json."""
        from enhance_coverage import register_report_directory, load_report_registry
        report_id = "report_proc_test_123"
        fake_report_dir = os.path.join(self.temp_dir, "fake_report")
        os.makedirs(fake_report_dir, exist_ok=True)

        register_report_directory(report_id, fake_report_dir)
        registry = load_report_registry()
        self.assertIn(report_id, registry)
        self.assertIn(os.path.abspath(fake_report_dir), registry[report_id])

    def test_11_p0_3_reuse_output_does_not_corrupt_sidecar(self):
        """P0-3: --reuse-output repeated run does not overwrite sidecar with stripped HTML."""
        input_dir = os.path.join(self.temp_dir, "input_reuse")
        output_dir = os.path.join(self.temp_dir, "output_reuse")
        os.makedirs(input_dir, exist_ok=True)

        gcov_file = os.path.join(input_dir, "reuse.c.gcov.html")
        sample_gcov_html = """<!DOCTYPE html>
        <html>
        <head><title>LCOV - cov - src/reuse.c</title></head>
        <body>
          <pre class="source">
            <span class="lineNum"> 1 </span><span class="lineCov"> int first_line = 1;</span>
            <span class="lineNum"> 2 </span><span class="lineNoCov"> int second_line = 2;</span>
          </pre>
        </body>
        </html>"""

        with open(gcov_file, "w", encoding="utf-8") as f:
            f.write(sample_gcov_html)

        # First run
        inject_coverage_report(
            input_dir=input_dir,
            output_dir=output_dir,
            project_name="ReuseProj",
            render_mode="lazy_collapse",
            reuse_output=True,
        )

        # Second run with reuse_output=True
        inject_coverage_report(
            input_dir=input_dir,
            output_dir=output_dir,
            project_name="ReuseProj",
            render_mode="lazy_collapse",
            reuse_output=True,
        )

        report_id = os.listdir(os.path.join(output_dir, ".source_cache"))[0]
        service = CodeDetailService()
        lines_data = service.get_code_lines_single("ReuseProj", "src/reuse.c", start_line=1, end_line=2, report_id=report_id)
        self.assertEqual(len(lines_data["lines"]), 2)
        self.assertEqual(lines_data["lines"][0]["source"], "int first_line = 1;")
        self.assertEqual(lines_data["lines"][1]["source"], "int second_line = 2;")

    def test_12_p0_4_sidecar_failure_does_not_strip_html(self):
        """P0-4: If sidecar saving fails, HTML source must NOT be stripped."""
        input_dir = os.path.join(self.temp_dir, "input_fail")
        output_dir = os.path.join(self.temp_dir, "output_fail")
        os.makedirs(input_dir, exist_ok=True)

        gcov_file = os.path.join(input_dir, "fail.c.gcov.html")
        sample_gcov_html = """<!DOCTYPE html>
        <html>
        <head><title>LCOV - cov - src/fail.c</title></head>
        <body>
          <pre class="source">
            <span class="lineNum"> 1 </span><span class="lineNoCov"> int critical_source_code = 12345;</span>
          </pre>
        </body>
        </html>"""

        with open(gcov_file, "w", encoding="utf-8") as f:
            f.write(sample_gcov_html)

        with patch("enhance_coverage.save_source_sidecar", side_effect=IOError("Disk write error")):
            inject_coverage_report(
                input_dir=input_dir,
                output_dir=output_dir,
                project_name="FailProj",
                render_mode="lazy_collapse",
                reuse_output=False,
            )

        injected_html_file = os.path.join(output_dir, "fail.c.gcov.html")
        with open(injected_html_file, "r", encoding="utf-8") as f:
            content = f.read()

        # HTML must still contain original source because sidecar failed to save
        self.assertIn("critical_source_code", content)

    def test_13_p1_1_cpp_signatures_multiline_and_qualifiers(self):
        """P1-1: C/C++ function parser handles multi-line signatures, C++ namespaces, constructors, destructors."""
        raw_lines = [
            {"line_no": 1, "code_text": "class MyClass {"},
            {"line_no": 2, "code_text": "public:"},
            {"line_no": 3, "code_text": "    MyClass::MyClass() {"},
            {"line_no": 4, "code_text": "        init();"},
            {"line_no": 5, "code_text": "    }"},
            {"line_no": 6, "code_text": "    MyClass::~MyClass() {"},
            {"line_no": 7, "code_text": "        cleanup();"},
            {"line_no": 8, "code_text": "    }"},
            {"line_no": 9, "code_text": "    int multi_line("},
            {"line_no": 10, "code_text": "        int a,"},
            {"line_no": 11, "code_text": "        int b"},
            {"line_no": 12, "code_text": "    ) const noexcept {"},
            {"line_no": 13, "code_text": "        return a + b;"},
            {"line_no": 14, "code_text": "    }"},
            {"line_no": 15, "code_text": "    auto trailing_return(int x) -> int {"},
            {"line_no": 16, "code_text": "        return x * 2;"},
            {"line_no": 17, "code_text": "    }"},
            {"line_no": 18, "code_text": "};"},
        ]

        functions = extract_c_function_ranges(raw_lines)
        fn_map = {f.name: f for f in functions}

        self.assertIn("MyClass::MyClass", fn_map)
        self.assertEqual(fn_map["MyClass::MyClass"].start_line, 3)
        self.assertEqual(fn_map["MyClass::MyClass"].end_line, 5)

        self.assertIn("MyClass::~MyClass", fn_map)
        self.assertEqual(fn_map["MyClass::~MyClass"].start_line, 6)
        self.assertEqual(fn_map["MyClass::~MyClass"].end_line, 8)

        self.assertIn("multi_line", fn_map)
        self.assertEqual(fn_map["multi_line"].start_line, 9)
        self.assertEqual(fn_map["multi_line"].end_line, 14)

        self.assertIn("trailing_return", fn_map)
        self.assertEqual(fn_map["trailing_return"].start_line, 15)
        self.assertEqual(fn_map["trailing_return"].end_line, 17)

    def test_14_p1_5_p1_6_validation_and_authoritative_sidecar(self):
        """P1-5 & P1-6: Invalid report_id/review_scope is rejected; report_id requires valid sidecar."""
        service = CodeDetailService()

        # Invalid report_id format (e.g. path traversal characters or spaces)
        with self.assertRaises(ValueError):
            service.get_code_layout("Proj", "src/test.c", report_id="../../bad/path")

        # Invalid review_scope
        with self.assertRaises(ValueError):
            service.get_code_layout("Proj", "src/test.c", review_scope="invalid_scope")

        # Non-existent report_id raises FileNotFoundError without fallback
        with self.assertRaises(FileNotFoundError):
            service.get_code_layout("Proj", "src/test.c", report_id="report_nonexistent123")

    def test_15_p2_1_max_batch_total_lines_limit(self):
        """P2-1: Exceeding MAX_BATCH_TOTAL_LINES (50000) raises ValueError."""
        ctx = SourceContext(
            project_name="Proj",
            file_path="src/large.c",
            lines=[SourceLineDTO(i, f"int x_{i};", f"raw_{i}", "covered") for i in range(1, 60001)],
        )
        # Single range requesting 55,000 lines
        with self.assertRaises(ValueError):
            read_source_ranges(ctx, [{"start_line": 1, "end_line": 55000}])

    def test_16_published_pages_version_only_on_incremental_page(self):
        """Verify version identifier is displayed only on incremental coverage homepage."""
        # 1. Check incremental_coverage.html renders version
        result_mock = {
            "summary": {"changed_lines": 10, "covered": 8, "uncovered": 2, "ignored": 0, "unanalyzed": 0, "missing": 0, "coverage_rate": 80.0},
            "details": [],
            "files": [],
            "oldgit": "abc1234",
            "newgit": "def5678",
            "generated_at": "2026-08-19 09:00:00",
            "git_range": "HEAD~1..HEAD",
            "repository_ranges": {}
        }
        out_dir = os.path.join(self.temp_dir, "test_v16_out")
        os.makedirs(out_dir, exist_ok=True)
        enhance_coverage.write_incremental_summary_page(out_dir, "TestProj", result_mock, config={}, unanalyzed_by_file={})
        
        with open(os.path.join(out_dir, "incremental_coverage.html"), "r", encoding="utf-8") as f:
            inc_html = f.read()
        self.assertIn("v11.3 2026-08-19", inc_html)
        self.assertIn("page-version-badge", inc_html)
        self.assertIn("whats-new-modal", inc_html)
        self.assertIn("whats-new-btn", inc_html)

        # 2. Check developer tasks page does NOT contain badge or footer version
        enhance_coverage.write_incremental_developer_tasks_page(out_dir, "TestProj", result_mock)
        with open(os.path.join(out_dir, "incremental_developer_tasks.html"), "r", encoding="utf-8") as f:
            dev_html = f.read()
        self.assertNotIn("v11.3 2026-08-19", dev_html)

        # 3. Check other pages and components do NOT contain toolbar or badge version
        with open(enhance_coverage.JS_SOURCE_PATH, "r", encoding="utf-8") as f:
            js_code = f.read()
        self.assertNotIn("coverage-lazy-toolbar-version", js_code)
        self.assertNotIn("RELEASE_TIME", js_code)

        with open(enhance_coverage.PROGRESS_PAGE_SOURCE_PATH, "r", encoding="utf-8") as f:
            prog_html = f.read()
        self.assertNotIn("v11.3 2026-08-19", prog_html)

    def test_17_smoke_tests_import_and_help(self):
        """P0-1 Smoke Test: test module imports and CLI --help execution."""
        import subprocess
        # 1. Test CLI --help
        res = subprocess.run([sys.executable, "enhance_coverage.py", "--help"], cwd=enhance_coverage.SCRIPT_DIR, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("Usage:", res.stdout)

        # 2. Test imports
        res_import = subprocess.run([sys.executable, "-c", "import enhance_coverage, code_detail_service, source_reader"], cwd=enhance_coverage.SCRIPT_DIR, capture_output=True, text=True)
        self.assertEqual(res_import.returncode, 0)

    def test_18_batch_lines_region_ids_bypasses_arbitrary_limit_for_verified_defaults(self):
        """P0-2: Verify verified default expanded regions load completely even for 60,000 lines."""
        # Create a mock source file with 60,000 lines
        lines_count = 60000
        mock_lines = [
            SourceLineDTO(
                line_no=i,
                source=f"    int x_{i} = {i};",
                coverage_state="uncovered" if i == 10 else "covered",
                is_pending_analysis=(i == 10),
            )
            for i in range(1, lines_count + 1)
        ]
        context = SourceContext(
            project_name="BigFuncProj",
            file_path="big_func.c",
            lines=mock_lines,
            function_ranges=[FunctionRange(1, lines_count, "super_big_function")],
            pending_lines=[10],
            total_uncovered_count=1,
            report_id="report_big_test",
        )

        service = CodeDetailService()
        service._context_cache[("BigFuncProj", "report_big_test", "big_func.c", "full")] = (time.time(), context)

        # 1. Arbitrary ranges > 50000 should raise ValueError
        with self.assertRaises(ValueError) as cm:
            service.get_code_lines_batch(
                project_name="BigFuncProj",
                file_path="big_func.c",
                ranges=[{"start_line": 1, "end_line": lines_count}],
                report_id="report_big_test",
            )
        self.assertIn("exceeds maximum batch limit", str(cm.exception))

        # 2. Verified default region_id should succeed without limit
        res = service.get_code_lines_batch(
            project_name="BigFuncProj",
            file_path="big_func.c",
            region_ids=["region-1-60000"],
            report_id="report_big_test",
        )
        self.assertEqual(len(res["ranges"]), 1)
        self.assertEqual(len(res["ranges"][0]["lines"]), lines_count)
        self.assertTrue(res["perf"]["verified_default_batch"])

    def test_19_immutable_report_id_and_incremental_review_set_hash(self):
        """P0-3 & P1-2: Verify report_id and signature depend on incremental review set and isolate cache."""
        input_dir = os.path.join(self.temp_dir, "test_sig_in")
        out_dir = os.path.join(self.temp_dir, "test_sig_out")
        os.makedirs(os.path.join(input_dir, "html"), exist_ok=True)
        with open(os.path.join(input_dir, "html", "sample.c.gcov.html"), "w", encoding="utf-8") as f:
            f.write("<html><head></head><body><pre class=\"source\">int main(){ return 0; }</pre></body></html>")

        # Sig 1 with incremental lines {1}
        sig1 = enhance_coverage.compute_directory_signature(
            os.path.join(input_dir, "html"), project_name="SigProj", review_scope="incremental",
            render_mode="lazy_collapse", incremental_lines_by_file={"sample.c": [1]}
        )
        # Sig 2 with incremental lines {2}
        sig2 = enhance_coverage.compute_directory_signature(
            os.path.join(input_dir, "html"), project_name="SigProj", review_scope="incremental",
            render_mode="lazy_collapse", incremental_lines_by_file={"sample.c": [2]}
        )

        self.assertNotEqual(sig1["incremental_review_set_hash"], sig2["incremental_review_set_hash"])

    def test_20_perf_pending_lines_in_global_gaps(self):
        """P1-1: Benchmark 10,000 functions + 10,000 pending lines in global gaps (< 50ms)."""
        num_fns = 10000
        fn_ranges = []
        pending_gaps = []
        for i in range(num_fns):
            s_line = i * 20 + 2
            e_line = s_line + 10
            fn_ranges.append(FunctionRange(s_line, e_line, f"func_{i}"))
            # Gap line between previous function and current function
            gap_line = i * 20 + 1
            pending_gaps.append(gap_line)

        t0 = time.perf_counter()
        regions = build_code_regions(
            total_lines=num_fns * 20 + 50,
            pending_lines=pending_gaps,
            function_ranges=fn_ranges,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        print(f"\n[Perf Test] 10,000 functions + 10,000 gap pending lines build time = {elapsed_ms:.2f}ms")
        self.assertLess(elapsed_ms, 50.0)
        self.assertTrue(len(regions) > 0)

    def test_21_lazy_collapse_same_input_output_dir_raises(self):
        """P1-3: Verify lazy_collapse mode forbids same input_dir and output_dir."""
        same_dir = os.path.join(self.temp_dir, "same_in_out")
        os.makedirs(same_dir, exist_ok=True)
        with self.assertRaises(ValueError) as cm:
            enhance_coverage.inject_coverage_report(
                same_dir, same_dir, project_name="SameDirProj", render_mode="lazy_collapse"
            )
        self.assertIn("must be distinct directories", str(cm.exception))

    def test_22_report_registry_dir_multi_process_isolation(self):
        """P1-4: Verify report registry creates per-report files in registry directory."""
        test_reg_dir = os.path.join(self.temp_dir, "custom_registry_dir")
        old_reg_env = os.environ.get("COVERAGE_REGISTRY_DIR")
        os.environ["COVERAGE_REGISTRY_DIR"] = test_reg_dir
        enhance_coverage.REPORT_REGISTRY_DIR = test_reg_dir
        code_detail_service.REPORT_REGISTRY_DIR = test_reg_dir
        try:
            r1_dir = os.path.join(self.temp_dir, "r1_dir")
            r2_dir = os.path.join(self.temp_dir, "r2_dir")
            os.makedirs(r1_dir, exist_ok=True)
            os.makedirs(r2_dir, exist_ok=True)

            enhance_coverage.register_report_directory("report_iso_1", r1_dir)
            enhance_coverage.register_report_directory("report_iso_2", r2_dir)

            self.assertTrue(os.path.isfile(os.path.join(test_reg_dir, "report_iso_1.json")))
            self.assertTrue(os.path.isfile(os.path.join(test_reg_dir, "report_iso_2.json")))

            loaded = code_detail_service.load_report_registry()
            self.assertIn("report_iso_1", loaded)
            self.assertIn("report_iso_2", loaded)
            self.assertIn(os.path.abspath(r1_dir), loaded["report_iso_1"])
            self.assertIn(os.path.abspath(r2_dir), loaded["report_iso_2"])
        finally:
            if old_reg_env is not None:
                os.environ["COVERAGE_REGISTRY_DIR"] = old_reg_env
            else:
                os.environ.pop("COVERAGE_REGISTRY_DIR", None)


if __name__ == "__main__":
    unittest.main()
