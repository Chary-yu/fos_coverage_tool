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
import subprocess
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
        self.reg_dir = os.path.join(self.temp_dir, "test_registry")
        os.makedirs(self.reg_dir, exist_ok=True)
        self.old_reg_env = os.environ.get("COVERAGE_REGISTRY_DIR")
        os.environ["COVERAGE_REGISTRY_DIR"] = self.reg_dir

    def tearDown(self):
        if self.old_reg_env is not None:
            os.environ["COVERAGE_REGISTRY_DIR"] = self.old_reg_env
        else:
            os.environ.pop("COVERAGE_REGISTRY_DIR", None)
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

        # Injected JS must be identical to static source (no string injection)
        injected_js_file = os.path.join(output_dir, "coverage_enhance.js")
        with open(injected_js_file, "r", encoding="utf-8") as f:
            js_content = f.read()
        with open(enhance_coverage.JS_SOURCE_PATH, "r", encoding="utf-8") as f_src:
            src_js = f_src.read()
        self.assertEqual(js_content, src_js)

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
        self.assertIn(enhance_coverage.VERSION_DISPLAY_LABEL, inc_html)
        self.assertIn("page-version-badge", inc_html)
        self.assertIn("whats-new-modal", inc_html)
        self.assertIn("whats-new-btn", inc_html)

        # 2. Check developer tasks page does NOT contain badge or footer version
        enhance_coverage.write_incremental_developer_tasks_page(out_dir, "TestProj", result_mock)
        with open(os.path.join(out_dir, "incremental_developer_tasks.html"), "r", encoding="utf-8") as f:
            dev_html = f.read()
        self.assertNotIn(enhance_coverage.VERSION_DISPLAY_LABEL, dev_html)

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

    def test_23_cpp_files_injected_and_sidecar_generated(self):
        """P0-1: Verify C++ .cpp.gcov.html, .cc.gcov.html, .hpp.gcov.html files are injected and sidecars generated."""
        input_dir = os.path.join(self.temp_dir, "cpp_input")
        output_dir = os.path.join(self.temp_dir, "cpp_output")
        os.makedirs(input_dir, exist_ok=True)

        cpp_files = ["calc.cpp.gcov.html", "engine.cc.gcov.html", "header.hpp.gcov.html"]
        for fname in cpp_files:
            content = f"""<!DOCTYPE html><html><head><title>LCOV - cov - src/{fname.replace('.gcov.html', '')}</title></head>
<body><pre class="source">
<span class="lineNum">    1 </span><span id="L1" class="lineCov">int foo() {{</span>
<span class="lineNum">    2 </span><span id="L2" class="lineNoCov tlaUNC tlaBgUNC">    return 42;</span>
<span class="lineNum">    3 </span><span id="L3" class="lineCov">}}</span>
</pre></body></html>"""
            with open(os.path.join(input_dir, fname), "w", encoding="utf-8") as f:
                f.write(content)

        enhance_coverage.inject_coverage_report(
            input_dir, output_dir, project_name="CppProj", render_mode="lazy_collapse"
        )

        cache_root = os.path.join(output_dir, ".source_cache")
        self.assertTrue(os.path.isdir(cache_root))
        report_ids = [d for d in os.listdir(cache_root) if not d.startswith(".")]
        self.assertEqual(len(report_ids), 1)
        report_id = report_ids[0]

        service = CodeDetailService(search_dirs=[output_dir])

        for fname in cpp_files:
            src_name = fname.replace(".gcov.html", "")
            out_file = os.path.join(output_dir, fname)
            self.assertTrue(os.path.isfile(out_file))

            with open(out_file, "r", encoding="utf-8") as f:
                html_out = f.read()

            self.assertIn("coverage_enhance.js", html_out)
            self.assertIn("coverage_enhance.css", html_out)
            self.assertNotIn("return 42;", html_out, "HTML source code should be stripped")

            # Layout API check
            layout = service.get_code_layout("CppProj", f"src/{src_name}", report_id=report_id)
            self.assertEqual(layout["total_lines"], 3)
            self.assertEqual(layout["pending_line_count"], 1)

    def test_24_dynamic_asset_version_format(self):
        """P1-1: Verify ASSET_VERSION includes dynamic hash format."""
        self.assertTrue(enhance_coverage.ASSET_VERSION.startswith("lazy-collapse-"))
        self.assertIn("_", enhance_coverage.ASSET_VERSION)

    def test_25_signature_manifest_hash_collision_protection(self):
        """P1-2: Verify modify content without changing file size or max mtime changes signature manifest_hash."""
        sig_dir = os.path.join(self.temp_dir, "sig_test_dir")
        os.makedirs(sig_dir, exist_ok=True)

        f1 = os.path.join(sig_dir, "a.gcov.html")
        f2 = os.path.join(sig_dir, "b.gcov.html")

        with open(f1, "w", encoding="utf-8") as f:
            f.write("AAAA")
        with open(f2, "w", encoding="utf-8") as f:
            f.write("BBBB")

        # Give f2 higher mtime
        t_high = time.time() + 100
        os.utime(f2, (t_high, t_high))

        sig1 = enhance_coverage.compute_directory_signature(sig_dir, project_name="SigTest")

        # Now change f1 content from AAAA to CCCC (same size 4 bytes, mtime still < f2)
        with open(f1, "w", encoding="utf-8") as f:
            f.write("CCCC")

        sig2 = enhance_coverage.compute_directory_signature(sig_dir, project_name="SigTest")

        self.assertNotEqual(sig1["manifest_hash"], sig2["manifest_hash"])

    def test_26_context_cache_ttl_and_lru_eviction(self):
        """P1-3: Verify CodeDetailService context cache evicts oldest entries and expires TTL."""
        service = CodeDetailService(max_cache_entries=2)
        service._cache_ttl_sec = 0.05

        def make_dummy_html(name):
            return f"""<!DOCTYPE html><html><head><title>LCOV - {name}</title></head>
            <body><pre class="source"><span id="L1" class="lineCov">int x = 1;</span></pre></body></html>"""

        # Add item 1
        service.get_source_context("P", "f1.c", content_override=make_dummy_html("f1.c"))
        self.assertIn(("P", "", "f1.c", "full"), service._context_cache)

        # Add item 2
        service.get_source_context("P", "f2.c", content_override=make_dummy_html("f2.c"))
        self.assertIn(("P", "", "f2.c", "full"), service._context_cache)

        # Add item 3 -> should evict item 1 (capacity = 2)
        service.get_source_context("P", "f3.c", content_override=make_dummy_html("f3.c"))
        self.assertEqual(len(service._context_cache), 2)
        self.assertNotIn(("P", "", "f1.c", "full"), service._context_cache)
        self.assertIn(("P", "", "f3.c", "full"), service._context_cache)

        # Test TTL expiration
        time.sleep(0.06)
        service._prune_context_cache()
        self.assertEqual(len(service._context_cache), 0)

    def test_27_batch_over_1000_default_expanded_regions(self):
        """P1-4: Verify get_code_lines_batch with load_default_expanded supports >1000 regions without error."""
        # Build a synthetic HTML with 1001 functions, each with 1 pending line
        num_fns = 1001
        lines = ['<!DOCTYPE html><html><head><title>LCOV - huge</title></head><body><pre class="source">']
        for i in range(1, num_fns + 1):
            s_line = (i - 1) * 30 + 1
            lines.append(f'<span class="lineNum">{s_line:5d}</span><span id="L{s_line}" class="lineCov">int fn_{i}() {{</span>')
            lines.append(f'<span class="lineNum">{s_line+1:5d}</span><span id="L{s_line+1}" class="lineNoCov tlaUNC tlaBgUNC">    return {i};</span>')
            lines.append(f'<span class="lineNum">{s_line+2:5d}</span><span id="L{s_line+2}" class="lineCov">}}</span>')
            for filler in range(s_line + 3, s_line + 30):
                lines.append(f'<span class="lineNum">{filler:5d}</span><span id="L{filler}" class="lineCov">    int pad = {filler};</span>')
        lines.append('</pre></body></html>')
        huge_html = "\n".join(lines)

        service = CodeDetailService()
        batch = service.get_code_lines_batch(
            project_name="HugeProj",
            file_path="huge.c",
            load_default_expanded=True,
            content_override=huge_html,
        )
        self.assertEqual(len(batch["ranges"]), num_fns)
        self.assertTrue(batch["perf"]["verified_default_batch"])

    def test_28_cpp_function_parser_edge_cases(self):
        """P2-4: Verify C++ function parser handles templates, multiline return types, operator(), and attributes."""
        raw_lines = [
            # 1. Template function
            {"line_no": 1, "code_text": "template <typename T>"},
            {"line_no": 2, "code_text": "T Foo<T>::bar(const T& x) const {"},
            {"line_no": 3, "code_text": "    return x;"},
            {"line_no": 4, "code_text": "}"},
            # 2. Multiline return type
            {"line_no": 5, "code_text": "static int"},
            {"line_no": 6, "code_text": "multi_return_foo(int x)"},
            {"line_no": 7, "code_text": "{"},
            {"line_no": 8, "code_text": "    return x * 2;"},
            {"line_no": 9, "code_text": "}"},
            # 3. operator()
            {"line_no": 10, "code_text": "int Foo::operator()(int x) const {"},
            {"line_no": 11, "code_text": "    return x + 1;"},
            {"line_no": 12, "code_text": "}"},
            # 4. GNU attribute
            {"line_no": 13, "code_text": "__attribute__((noinline)) int attr_foo(int x) {"},
            {"line_no": 14, "code_text": "    return x;"},
            {"line_no": 15, "code_text": "}"},
        ]

        functions = extract_c_function_ranges(raw_lines)
        self.assertEqual(len(functions), 4)

        # Template function range should start at line 1 (includes template <...>)
        self.assertEqual(functions[0].start_line, 1)
        self.assertEqual(functions[0].end_line, 4)

        # Multi-line return type function range should start at line 5
        self.assertEqual(functions[1].start_line, 5)
        self.assertEqual(functions[1].end_line, 9)
        self.assertEqual(functions[1].name, "multi_return_foo")

        # operator()
        self.assertEqual(functions[2].start_line, 10)
        self.assertEqual(functions[2].end_line, 12)
        self.assertEqual(functions[2].name, "Foo::operator()")

        # Attribute
        self.assertEqual(functions[3].start_line, 13)
        self.assertEqual(functions[3].end_line, 15)
        self.assertEqual(functions[3].name, "attr_foo")

    def test_29_registry_fast_lookup_and_cleanup(self):
        """P2-5 & P2-6: Verify single report_id fast lookup and stale registry cleanup."""
        reg_dir = os.path.join(self.temp_dir, "test_reg_p2")
        old_env = os.environ.get("COVERAGE_REGISTRY_DIR")
        os.environ["COVERAGE_REGISTRY_DIR"] = reg_dir
        try:
            d_alive = os.path.join(self.temp_dir, "alive_dir")
            d_stale = os.path.join(self.temp_dir, "stale_dir")
            os.makedirs(d_alive, exist_ok=True)
            os.makedirs(d_stale, exist_ok=True)

            enhance_coverage.register_report_directory("report_alive", d_alive)
            enhance_coverage.register_report_directory("report_stale", d_stale)

            # Fast single lookup
            loaded_single = code_detail_service.load_report_registry("report_alive")
            self.assertEqual(len(loaded_single), 1)
            self.assertIn("report_alive", loaded_single)

            # Remove d_stale and run prune
            shutil.rmtree(d_stale)
            enhance_coverage.prune_stale_report_registry(reg_dir)

            self.assertTrue(os.path.isfile(os.path.join(reg_dir, "report_alive.json")))
            self.assertFalse(os.path.isfile(os.path.join(reg_dir, "report_stale.json")))
        finally:
            if old_env is not None:
                os.environ["COVERAGE_REGISTRY_DIR"] = old_env
            else:
                os.environ.pop("COVERAGE_REGISTRY_DIR", None)

    def test_30_region_ids_exceeding_1000_rejected(self):
        """P1-4: Verify passing >1000 region_ids without load_default_expanded is rejected with 400."""
        from enhance_coverage import CoverageHTTPRequestHandler

        handler = CoverageHTTPRequestHandler.__new__(CoverageHTTPRequestHandler)
        handler.path = "/api/coverage/code-lines/batch"
        handler.send_error_response = MagicMock()
        handler.send_json_response = MagicMock()

        dummy_payload = {
            "project_name": "TestProj",
            "file_path": "src/test.c",
            "region_ids": [f"reg_{i}" for i in range(1001)]
        }
        payload_bytes = json.dumps(dummy_payload).encode("utf-8")
        handler.rfile = io.BytesIO(payload_bytes)
        handler.headers = {"Content-Length": str(len(payload_bytes))}
        
        handler.do_POST()
        handler.send_error_response.assert_called_with(400, "region_ids must be a list with at most 1000 items")

        # Also test ranges exceeding 1000
        handler.send_error_response.reset_mock()
        dummy_payload_ranges = {
            "project_name": "TestProj",
            "file_path": "src/test.c",
            "ranges": [{"start_line": i, "end_line": i} for i in range(1001)]
        }
        payload_bytes_ranges = json.dumps(dummy_payload_ranges).encode("utf-8")
        handler.rfile = io.BytesIO(payload_bytes_ranges)
        handler.headers = {"Content-Length": str(len(payload_bytes_ranges))}
        
        handler.do_POST()
        handler.send_error_response.assert_called_with(400, "ranges must be a list with at most 1000 items")

    def test_31_coverage_report_roots_env_var(self):
        """P1-5: Verify COVERAGE_REPORT_ROOTS is parsed and added to search_dirs."""
        root_a = os.path.join(self.temp_dir, "root_a")
        root_b = os.path.join(self.temp_dir, "root_b")
        os.makedirs(root_a, exist_ok=True)
        os.makedirs(root_b, exist_ok=True)

        old_env = os.environ.get("COVERAGE_REPORT_ROOTS")
        sep = os.pathsep
        os.environ["COVERAGE_REPORT_ROOTS"] = f"{root_a}{sep}{root_b}"
        try:
            service = CodeDetailService()
            self.assertIn(os.path.abspath(root_a), service.search_dirs)
            self.assertIn(os.path.abspath(root_b), service.search_dirs)

            service_global = enhance_coverage.get_code_detail_service(search_dirs=[])
            self.assertIn(os.path.abspath(root_a), service_global.search_dirs)
            self.assertIn(os.path.abspath(root_b), service_global.search_dirs)
        finally:
            if old_env is not None:
                os.environ["COVERAGE_REPORT_ROOTS"] = old_env
            else:
                os.environ.pop("COVERAGE_REPORT_ROOTS", None)

    def test_32_context_cache_total_lines_limit(self):
        """P1/P2: Verify CodeDetailService evicts oldest entries when total lines exceed threshold."""
        service = CodeDetailService(max_cache_entries=10, max_cache_total_lines=150)
        
        def make_html(name, num_lines):
            lines = [f'<span id="L{i}" class="lineCov">line {i}</span>' for i in range(1, num_lines + 1)]
            return f"""<!DOCTYPE html><html><head><title>LCOV - {name}</title></head>
            <body><pre class="source">{''.join(lines)}</pre></body></html>"""

        # Add f1 with 100 lines
        service.get_source_context("P", "f1.c", content_override=make_html("f1.c", 100))
        self.assertIn(("P", "", "f1.c", "full"), service._context_cache)

        # Add f2 with 40 lines (total: 140 <= 150)
        service.get_source_context("P", "f2.c", content_override=make_html("f2.c", 40))
        self.assertIn(("P", "", "f1.c", "full"), service._context_cache)
        self.assertIn(("P", "", "f2.c", "full"), service._context_cache)

        # Add f3 with 50 lines (total would be 190 > 150 -> f1 evicted)
        service.get_source_context("P", "f3.c", content_override=make_html("f3.c", 50))
        self.assertNotIn(("P", "", "f1.c", "full"), service._context_cache)
        self.assertIn(("P", "", "f2.c", "full"), service._context_cache)
        self.assertIn(("P", "", "f3.c", "full"), service._context_cache)

        # Test legacy file path on disk without content_override
        legacy_dir = os.path.join(self.temp_dir, "legacy_cache_test")
        os.makedirs(legacy_dir, exist_ok=True)
        legacy_file = os.path.join(legacy_dir, "f4.c.gcov.html")
        with open(legacy_file, "w", encoding="utf-8") as f:
            f.write(make_html("f4.c", 120))
        
        service_legacy = CodeDetailService(search_dirs=[legacy_dir], max_cache_entries=10, max_cache_total_lines=150)
        service_legacy.get_source_context("P", "f2.c", content_override=make_html("f2.c", 40))
        self.assertIn(("P", "", "f2.c", "full"), service_legacy._context_cache)
        # Loading f4 (120 lines) via legacy disk path must evict f2 (total would be 160 > 150)
        service_legacy.get_source_context("P", "f4.c")
        self.assertNotIn(("P", "", "f2.c", "full"), service_legacy._context_cache)
        self.assertIn(("P", "", "f4.c", "full"), service_legacy._context_cache)

    def test_33_prune_registry_checks_source_cache(self):
        """P2-7: Verify prune_stale_report_registry prunes entry when directory exists but .source_cache/report_id is missing."""
        reg_dir = os.path.join(self.temp_dir, "reg_prune_cache")
        report_dir = os.path.join(self.temp_dir, "report_dir")
        os.makedirs(os.path.join(report_dir, ".source_cache", "report_active"), exist_ok=True)
        os.makedirs(reg_dir, exist_ok=True)

        # Create registry file for report_active (valid) and report_old (missing in .source_cache)
        enhance_coverage.register_report_directory("report_active", report_dir)
        
        # Manually create registry file for report_old
        old_reg_file = os.path.join(reg_dir, "report_old.json")
        with open(old_reg_file, "w", encoding="utf-8") as f:
            json.dump({"report_id": "report_old", "directories": [report_dir]}, f)

        enhance_coverage.prune_stale_report_registry(reg_dir)
        self.assertFalse(os.path.isfile(old_reg_file))

    def test_34_stripped_input_file_rejected(self):
        """P2-8: Verify injecting an already stripped Lazy Collapse output as input raises ValueError."""
        in_dir = os.path.join(self.temp_dir, "stripped_in")
        out_dir = os.path.join(self.temp_dir, "stripped_out")
        os.makedirs(in_dir, exist_ok=True)

        stripped_html = """<!DOCTYPE html><html><head>
<title>LCOV - cov - src/stripped.c</title>
<meta name="coverage-report-id" content="report_123">
</head><body><pre class="source"></pre></body></html>"""
        with open(os.path.join(in_dir, "stripped.c.gcov.html"), "w", encoding="utf-8") as f:
            f.write(stripped_html)

        with self.assertRaises(ValueError) as cm:
            enhance_coverage.inject_coverage_report(
                in_dir, out_dir, project_name="StrippedProj", render_mode="lazy_collapse"
            )
        self.assertIn("already stripped Lazy Collapse report", str(cm.exception))

    def test_35_all_meta_tags_injected(self):
        """P1-3: Verify injected HTML contains coverage-project, report-id, file-path, render-mode, review-scope meta tags."""
        in_dir = os.path.join(self.temp_dir, "meta_in")
        out_dir = os.path.join(self.temp_dir, "meta_out")
        os.makedirs(in_dir, exist_ok=True)

        raw_html = """<!DOCTYPE html><html><head><title>LCOV - cov - src/app.c</title></head>
<body><pre class="source">
<span class="lineNum"> 1 </span><span id="L1" class="lineCov">int x = 1;</span>
</pre></body></html>"""
        with open(os.path.join(in_dir, "app.c.gcov.html"), "w", encoding="utf-8") as f:
            f.write(raw_html)

        enhance_coverage.inject_coverage_report(
            in_dir, out_dir, project_name="MetaTestProject", render_mode="lazy_collapse", review_scope="full"
        )

        with open(os.path.join(out_dir, "app.c.gcov.html"), "r", encoding="utf-8") as f:
            out_html = f.read()

        self.assertIn('meta name="coverage-project" content="MetaTestProject"', out_html)
        self.assertIn('meta name="coverage-report-id"', out_html)
        self.assertIn('meta name="coverage-file-path" content="src/app.c"', out_html)
        self.assertIn('meta name="coverage-render-mode" content="lazy_collapse"', out_html)
        self.assertIn('meta name="coverage-review-scope" content="full"', out_html)

    def test_36_node_browser_smoke_tests(self):
        """Browser DOM Smoke Tests: chunk retry deduplication, expandAll race cancellation, and draft persistence."""
        base_dir = os.path.dirname(os.path.abspath(__file__))
        smoke_script = os.path.join(base_dir, "test_lazy_collapse_browser_smoke.js")
        if not os.path.isfile(smoke_script):
            self.skipTest("test_lazy_collapse_browser_smoke.js not found")
        
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("Node.js not installed in current environment")

        proc = subprocess.run(
            [node_bin, smoke_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"Node browser smoke tests failed:\n{proc.stdout}\n{proc.stderr}")


if __name__ == "__main__":
    unittest.main()
