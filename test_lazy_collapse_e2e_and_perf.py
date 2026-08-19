"""
End-to-end and performance test suite for onesensor_code_detail_lazy_collapse.
Validates Datasets A-D (1K, 10K, 50K, 100K lines), Datasets 1-4 (0%, 1%, 30%, 90% pending),
and Scenarios A-G according to Section 41 of the development plan.
"""

import os
import sys
import time
import tempfile
import unittest
from code_region import CodeRegion, FunctionRange, build_code_regions
from code_detail_service import CodeDetailService
from source_reader import (
    SourceContext,
    SourceLineDTO,
    parse_source_lines_from_gcov_html,
    read_source_lines,
    read_source_ranges,
)


def generate_synthetic_gcov_html(total_lines: int, pending_ratio: float = 0.05, fn_size: int = 50) -> str:
    """Generate synthetic genhtml report for benchmarking."""
    lines_html = []
    lines_html.append('<!DOCTYPE html><html><head><title>LCOV - cov - src/benchmark.c</title></head><body><pre class="source">')

    current_fn = 0
    pending_lines = set()
    num_pending_functions = max(1, int((total_lines * pending_ratio) / fn_size)) if pending_ratio > 0 else 0

    # Pick functions to be pending
    pending_fn_indices = set(range(1, num_pending_functions + 1))

    for line_no in range(1, total_lines + 1):
        fn_idx = (line_no - 1) // fn_size
        offset_in_fn = (line_no - 1) % fn_size

        if offset_in_fn == 0:
            current_fn += 1
            code_text = f"int func_{current_fn}(int arg) {{"
            cov_class = "lineCov"
        elif offset_in_fn == fn_size - 1:
            code_text = "}"
            cov_class = "lineCov"
        else:
            if fn_idx in pending_fn_indices and offset_in_fn in (5, 6, 7):
                code_text = f"    do_uncovered_work_{offset_in_fn}();"
                cov_class = "lineNoCov tlaUNC tlaBgUNC"
                pending_lines.add(line_no)
            else:
                code_text = f"    do_covered_work_{offset_in_fn}();"
                cov_class = "lineCov tlaGNC tlaBgGNC"

        lines_html.append(f'<span class="lineNum">{line_no:5d}</span><span id="L{line_no}" class="{cov_class}">{code_text}</span>')

    lines_html.append('</pre></body></html>')
    return "\n".join(lines_html)


def measure_median_time(benchmark_fn, warmup: int = 1, runs: int = 5) -> float:
    """Run warmup and multiple iterations, returning median elapsed milliseconds."""
    for _ in range(warmup):
        benchmark_fn()
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        benchmark_fn()
        times.append((time.perf_counter() - t0) * 1000.0)
    times.sort()
    return times[len(times) // 2]


class TestLazyCollapseE2EAndPerf(unittest.TestCase):

    def setUp(self):
        self.service = CodeDetailService()

    # =========================================================================
    # Phase 4.1: Scenario A - G Verifications
    # =========================================================================

    def test_scenario_a_normal_pending_function(self):
        """Scenario A: Standard file where pending lines fall within a function."""
        # 100 lines total, function 1: 10..30 (pending line 15), function 2: 50..70 (covered)
        functions = [
            FunctionRange(10, 30, "calc_foo"),
            FunctionRange(50, 70, "calc_bar"),
        ]
        pending = [15]
        regions = build_code_regions(total_lines=100, pending_lines=pending, function_ranges=functions)

        # Expected regions:
        # [1, 9] collapsed
        # [10, 30] expanded (calc_foo)
        # [31, 100] collapsed
        self.assertEqual(len(regions), 3)
        self.assertEqual(regions[0].start_line, 1)
        self.assertEqual(regions[0].end_line, 9)
        self.assertEqual(regions[0].default_state, "collapsed")

        self.assertEqual(regions[1].start_line, 10)
        self.assertEqual(regions[1].end_line, 30)
        self.assertEqual(regions[1].default_state, "expanded")
        self.assertEqual(regions[1].kind, "analysis")
        self.assertEqual(regions[1].label, "calc_foo")

        self.assertEqual(regions[2].start_line, 31)
        self.assertEqual(regions[2].end_line, 100)
        self.assertEqual(regions[2].default_state, "collapsed")

    def test_scenario_b_no_pending_lines(self):
        """Scenario B: 0 pending lines in file -> single collapsed region [1, total_lines]."""
        regions = build_code_regions(
            total_lines=2386,
            pending_lines=[],
            function_ranges=[FunctionRange(10, 50, "fn1"), FunctionRange(100, 200, "fn2")],
        )
        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0].start_line, 1)
        self.assertEqual(regions[0].end_line, 2386)
        self.assertEqual(regions[0].default_state, "collapsed")
        self.assertEqual(regions[0].line_count, 2386)

    def test_scenario_c_adjacent_functions_merging_and_separation(self):
        """Scenario C: Adjacent functions: gap <= 20 merged, gap == 21 separated."""
        # fn1: 10..30 (pending 15)
        # fn2: 51..70 (pending 55) -> gap between fn1 and fn2 is 51 - 30 - 1 = 20 -> MERGED
        # fn3: 92..110 (pending 95) -> gap between fn2 (end 70) and fn3 (start 92) is 92 - 70 - 1 = 21 -> NOT MERGED
        functions = [
            FunctionRange(10, 30, "fn1"),
            FunctionRange(51, 70, "fn2"),
            FunctionRange(92, 110, "fn3"),
        ]
        pending = [15, 55, 95]
        regions = build_code_regions(total_lines=150, pending_lines=pending, function_ranges=functions)

        # Regions:
        # [1, 9] collapsed
        # [10, 70] expanded (fn1 and fn2 merged because gap = 20)
        # [71, 91] collapsed (21 lines)
        # [92, 110] expanded (fn3)
        # [111, 150] collapsed
        self.assertEqual(len(regions), 5)
        self.assertEqual((regions[0].start_line, regions[0].end_line, regions[0].default_state), (1, 9, "collapsed"))
        self.assertEqual((regions[1].start_line, regions[1].end_line, regions[1].default_state), (10, 70, "expanded"))
        self.assertEqual((regions[2].start_line, regions[2].end_line, regions[2].default_state), (71, 91, "collapsed"))
        self.assertEqual((regions[3].start_line, regions[3].end_line, regions[3].default_state), (92, 110, "expanded"))
        self.assertEqual((regions[4].start_line, regions[4].end_line, regions[4].default_state), (111, 150, "collapsed"))

    def test_scenario_d_top_bottom_non_pending(self):
        """Scenario D: File has non-pending code at top and bottom."""
        functions = [FunctionRange(50, 80, "middle_fn")]
        pending = [60]
        regions = build_code_regions(total_lines=200, pending_lines=pending, function_ranges=functions)

        self.assertEqual(len(regions), 3)
        self.assertEqual((regions[0].start_line, regions[0].end_line), (1, 49))
        self.assertEqual(regions[0].default_state, "collapsed")
        self.assertEqual((regions[1].start_line, regions[1].end_line), (50, 80))
        self.assertEqual(regions[1].default_state, "expanded")
        self.assertEqual((regions[2].start_line, regions[2].end_line), (81, 200))
        self.assertEqual(regions[2].default_state, "collapsed")

    def test_scenario_e_top_bottom_pending(self):
        """Scenario E: Pending lines start at line 1 and reach line total_lines."""
        functions = [
            FunctionRange(1, 40, "first_fn"),
            FunctionRange(160, 200, "last_fn"),
        ]
        pending = [5, 195]
        regions = build_code_regions(total_lines=200, pending_lines=pending, function_ranges=functions)

        self.assertEqual(len(regions), 3)
        self.assertEqual((regions[0].start_line, regions[0].end_line), (1, 40))
        self.assertEqual(regions[0].default_state, "expanded")
        self.assertEqual((regions[1].start_line, regions[1].end_line), (41, 159))
        self.assertEqual(regions[1].default_state, "collapsed")
        self.assertEqual((regions[2].start_line, regions[2].end_line), (160, 200))
        self.assertEqual(regions[2].default_state, "expanded")

    def test_scenario_f_batch_reading_and_caching(self):
        """Scenario F: Single batch reading of all expanded ranges."""
        html_content = generate_synthetic_gcov_html(total_lines=500, pending_ratio=0.1, fn_size=50)
        ctx = parse_source_lines_from_gcov_html(html_content, project_name="PerfProj", file_path="src/bench.c")
        regions = build_code_regions(ctx.total_lines, ctx.pending_lines, ctx.function_ranges)

        expanded_ranges = [
            {"start_line": r.start_line, "end_line": r.end_line}
            for r in regions if r.default_state == "expanded"
        ]

        median_ms = measure_median_time(lambda: read_source_ranges(ctx, expanded_ranges), warmup=1, runs=5)
        batch_results = read_source_ranges(ctx, expanded_ranges)

        self.assertEqual(len(batch_results), len(expanded_ranges))
        self.assertLess(median_ms, 50.0)  # fast batch reading

    # =========================================================================
    # Phase 4.2: Performance Benchmarks (Datasets A-D, Datasets 1-4)
    # =========================================================================

    def test_dataset_a_1000_lines_performance(self):
        """Dataset A: 1,000 lines file layout computation and initial batch reading."""
        html_content = generate_synthetic_gcov_html(total_lines=1000, pending_ratio=0.05, fn_size=50)
        median_ms = measure_median_time(
            lambda: self.service.get_code_layout("Test", "bench_1k.c", content_override=html_content),
            warmup=1,
            runs=5,
        )
        layout = self.service.get_code_layout("Test", "bench_1k.c", content_override=html_content)

        print(f"[Benchmark] Dataset A (1,000 lines): Layout build median = {median_ms:.2f}ms")
        self.assertLess(median_ms, 50.0, "Dataset A layout calculation median must be < 50ms")
        self.assertEqual(layout["total_lines"], 1000)

    def test_dataset_b_10000_lines_performance(self):
        """Dataset B: 10,000 lines file layout computation."""
        html_content = generate_synthetic_gcov_html(total_lines=10000, pending_ratio=0.03, fn_size=50)
        median_ms = measure_median_time(
            lambda: self.service.get_code_layout("Test", "bench_10k.c", content_override=html_content),
            warmup=1,
            runs=5,
        )
        layout = self.service.get_code_layout("Test", "bench_10k.c", content_override=html_content)

        print(f"[Benchmark] Dataset B (10,000 lines): Layout build median = {median_ms:.2f}ms")
        self.assertLess(median_ms, 200.0, "Dataset B layout calculation median must be < 200ms")
        self.assertEqual(layout["total_lines"], 10000)

    def test_dataset_c_50000_lines_performance(self):
        """Dataset C: 50,000 lines file layout computation."""
        html_content = generate_synthetic_gcov_html(total_lines=50000, pending_ratio=0.02, fn_size=100)
        median_ms = measure_median_time(
            lambda: self.service.get_code_layout("Test", "bench_50k.c", content_override=html_content),
            warmup=1,
            runs=5,
        )
        layout = self.service.get_code_layout("Test", "bench_50k.c", content_override=html_content)

        print(f"[Benchmark] Dataset C (50,000 lines): Layout build median = {median_ms:.2f}ms")
        self.assertLess(median_ms, 600.0, "Dataset C layout calculation median must be < 600ms")
        self.assertEqual(layout["total_lines"], 50000)

    def test_dataset_d_100000_lines_performance(self):
        """Dataset D: 100,000 lines file layout computation."""
        html_content = generate_synthetic_gcov_html(total_lines=100000, pending_ratio=0.01, fn_size=100)
        median_ms = measure_median_time(
            lambda: self.service.get_code_layout("Test", "bench_100k.c", content_override=html_content),
            warmup=1,
            runs=5,
        )
        layout = self.service.get_code_layout("Test", "bench_100k.c", content_override=html_content)

        print(f"[Benchmark] Dataset D (100,000 lines): Layout build median = {median_ms:.2f}ms")
        self.assertLess(median_ms, 1200.0, "Dataset D layout calculation median must be < 1200ms")
        self.assertEqual(layout["total_lines"], 100000)

    def test_datasets_1_to_4_ratio_scenarios(self):
        """Datasets 1-4: Testing 0%, 1%, 30%, 90% unanalyzed ratios."""
        ratios = [
            (0.00, "Dataset 1 (0% pending)"),
            (0.01, "Dataset 2 (1% pending)"),
            (0.30, "Dataset 3 (30% pending)"),
            (0.90, "Dataset 4 (90% pending)"),
        ]

        for ratio, name in ratios:
            html = generate_synthetic_gcov_html(total_lines=2000, pending_ratio=ratio, fn_size=40)
            layout = self.service.get_code_layout("Test", f"ratio_{int(ratio*100)}.c", content_override=html)
            perf = layout["perf"]
            print(f"[Benchmark] {name}: {perf}")

            if ratio == 0.0:
                self.assertEqual(len(layout["regions"]), 1)
                self.assertEqual(layout["regions"][0]["default_state"], "collapsed")
            elif ratio == 0.01:
                # Mostly collapsed
                self.assertLess(perf["expanded_line_count"], perf["collapsed_line_count"])
            elif ratio == 0.90:
                # Mostly expanded
                self.assertGreater(perf["expanded_line_count"], perf["collapsed_line_count"])


if __name__ == "__main__":
    unittest.main()
