"""
Unit tests for CodeRegionBuilder (code_region.py)
Covering all test cases specified in Section 27.1 of the development plan.
"""

import unittest
from code_region import (
    CodeRegion,
    FunctionRange,
    build_code_regions,
    find_function_containing_line,
    sanitize_function_ranges,
)


class TestCodeRegionBuilder(unittest.TestCase):

    def assertContinuousCoverage(self, regions, total_lines):
        """Verify that regions cover exactly 1..total_lines with no holes and no overlaps."""
        self.assertTrue(len(regions) > 0, "Regions list should not be empty")
        self.assertEqual(regions[0].start_line, 1, "First region must start at line 1")
        self.assertEqual(regions[-1].end_line, total_lines, f"Last region must end at {total_lines}")

        for i in range(len(regions)):
            cur = regions[i]
            self.assertTrue(cur.start_line <= cur.end_line, f"Region {cur} has invalid range")
            if i > 0:
                prev = regions[i - 1]
                self.assertEqual(
                    cur.start_line,
                    prev.end_line + 1,
                    f"Gap or overlap between {prev} and {cur}",
                )

    # Case 1: No pending line -> whole file collapsed
    def test_case_1_no_pending_line(self):
        regions = build_code_regions(total_lines=2386, pending_lines=[], function_ranges=[])
        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0].start_line, 1)
        self.assertEqual(regions[0].end_line, 2386)
        self.assertEqual(regions[0].default_state, "collapsed")
        self.assertEqual(regions[0].kind, "collapsed")
        self.assertIsNone(regions[0].label)
        self.assertContinuousCoverage(regions, 2386)

    # Case 2: One pending function
    def test_case_2_single_pending_function(self):
        functions = [FunctionRange(100, 250, "calculate_sum()")]
        regions = build_code_regions(
            total_lines=1000, pending_lines=[150], function_ranges=functions
        )
        self.assertEqual(len(regions), 3)
        self.assertEqual(regions[0], CodeRegion("region-1-99", 1, 99, "collapsed", "collapsed", None))
        self.assertEqual(regions[1], CodeRegion("region-100-250", 100, 250, "expanded", "analysis", "calculate_sum()"))
        self.assertEqual(regions[2], CodeRegion("region-251-1000", 251, 1000, "collapsed", "collapsed", None))
        self.assertContinuousCoverage(regions, 1000)

    # Case 3: Multiple pending functions
    def test_case_3_multiple_pending_functions(self):
        functions = [
            FunctionRange(100, 200, "foo()"),
            FunctionRange(500, 600, "bar()"),
        ]
        regions = build_code_regions(
            total_lines=1000, pending_lines=[120, 550], function_ranges=functions
        )
        self.assertEqual(len(regions), 5)
        self.assertEqual(regions[0], CodeRegion("region-1-99", 1, 99, "collapsed", "collapsed"))
        self.assertEqual(regions[1], CodeRegion("region-100-200", 100, 200, "expanded", "analysis", "foo()"))
        self.assertEqual(regions[2], CodeRegion("region-201-499", 201, 499, "collapsed", "collapsed"))
        self.assertEqual(regions[3], CodeRegion("region-500-600", 500, 600, "expanded", "analysis", "bar()"))
        self.assertEqual(regions[4], CodeRegion("region-601-1000", 601, 1000, "collapsed", "collapsed"))
        self.assertContinuousCoverage(regions, 1000)

    # Case 4: Duplicate pending lines in same function -> single expanded range
    def test_case_4_duplicate_pending_lines_same_function(self):
        functions = [FunctionRange(100, 200, "foo()")]
        regions = build_code_regions(
            total_lines=500, pending_lines=[110, 120, 150, 190], function_ranges=functions
        )
        self.assertEqual(len(regions), 3)
        self.assertEqual(regions[1], CodeRegion("region-100-200", 100, 200, "expanded", "analysis", "foo()"))
        self.assertContinuousCoverage(regions, 500)

    # Case 5: Two functions with gap = 20 -> MERGE
    def test_case_5_functions_gap_20_merged(self):
        # Function 1: 100..180 (end=180)
        # Function 2: 201..300 (start=201)
        # Gap = 201 - 180 - 1 = 20 -> Must merge into 100..300
        functions = [
            FunctionRange(100, 180, "func_a()"),
            FunctionRange(201, 300, "func_b()"),
        ]
        regions = build_code_regions(
            total_lines=500, pending_lines=[150, 250], function_ranges=functions
        )
        self.assertEqual(len(regions), 3)
        self.assertEqual(regions[0], CodeRegion("region-1-99", 1, 99, "collapsed", "collapsed"))
        self.assertEqual(regions[1].start_line, 100)
        self.assertEqual(regions[1].end_line, 300)
        self.assertEqual(regions[1].default_state, "expanded")
        self.assertIn("func_a()", regions[1].label)
        self.assertIn("func_b()", regions[1].label)
        self.assertEqual(regions[2], CodeRegion("region-301-500", 301, 500, "collapsed", "collapsed"))
        self.assertContinuousCoverage(regions, 500)

    # Case 6: Two functions with gap = 21 -> NOT MERGE
    def test_case_6_functions_gap_21_not_merged(self):
        # Function 1: 100..180 (end=180)
        # Function 2: 202..300 (start=202)
        # Gap = 202 - 180 - 1 = 21 -> Must NOT merge
        functions = [
            FunctionRange(100, 180, "func_a()"),
            FunctionRange(202, 300, "func_b()"),
        ]
        regions = build_code_regions(
            total_lines=500, pending_lines=[150, 250], function_ranges=functions
        )
        self.assertEqual(len(regions), 5)
        self.assertEqual(regions[0], CodeRegion("region-1-99", 1, 99, "collapsed", "collapsed"))
        self.assertEqual(regions[1], CodeRegion("region-100-180", 100, 180, "expanded", "analysis", "func_a()"))
        self.assertEqual(regions[2], CodeRegion("region-181-201", 181, 201, "collapsed", "collapsed"))
        self.assertEqual(regions[3], CodeRegion("region-202-300", 202, 300, "expanded", "analysis", "func_b()"))
        self.assertEqual(regions[4], CodeRegion("region-301-500", 301, 500, "collapsed", "collapsed"))
        self.assertContinuousCoverage(regions, 500)

    # Case 7: Pending line not in function -> +/- 20 fallback
    def test_case_7_fallback_plus_minus_20(self):
        regions = build_code_regions(
            total_lines=1000, pending_lines=[500], function_ranges=[]
        )
        self.assertEqual(len(regions), 3)
        self.assertEqual(regions[0], CodeRegion("region-1-479", 1, 479, "collapsed", "collapsed"))
        self.assertEqual(regions[1], CodeRegion("region-480-520", 480, 520, "expanded", "analysis", None))
        self.assertEqual(regions[2], CodeRegion("region-521-1000", 521, 1000, "collapsed", "collapsed"))
        self.assertContinuousCoverage(regions, 1000)

    # Case 8: Pending line near line 1 -> clips at line 1
    def test_case_8_fallback_near_start(self):
        regions = build_code_regions(
            total_lines=2000, pending_lines=[10], function_ranges=[]
        )
        self.assertEqual(len(regions), 2)
        # Line 10 fallback: max(1, 10-20)=1, min(2000, 10+20)=30 -> 1..30
        self.assertEqual(regions[0], CodeRegion("region-1-30", 1, 30, "expanded", "analysis", None))
        self.assertEqual(regions[1], CodeRegion("region-31-2000", 31, 2000, "collapsed", "collapsed"))
        self.assertContinuousCoverage(regions, 2000)

    # Case 9: Pending line near end -> clips at total_lines
    def test_case_9_fallback_near_end(self):
        regions = build_code_regions(
            total_lines=2000, pending_lines=[1995], function_ranges=[]
        )
        self.assertEqual(len(regions), 2)
        # Line 1995 fallback: max(1, 1995-20)=1975, min(2000, 1995+20)=2000 -> 1975..2000
        self.assertEqual(regions[0], CodeRegion("region-1-1974", 1, 1974, "collapsed", "collapsed"))
        self.assertEqual(regions[1], CodeRegion("region-1975-2000", 1975, 2000, "expanded", "analysis", None))
        self.assertContinuousCoverage(regions, 2000)

    # Case 10: Two fallback ranges overlap
    def test_case_10_fallback_ranges_overlap(self):
        # Line 100 -> 80..120
        # Line 130 -> 110..150
        # Overlapping -> merged into 80..150
        regions = build_code_regions(
            total_lines=1000, pending_lines=[100, 130], function_ranges=[]
        )
        self.assertEqual(len(regions), 3)
        self.assertEqual(regions[0], CodeRegion("region-1-79", 1, 79, "collapsed", "collapsed"))
        self.assertEqual(regions[1], CodeRegion("region-80-150", 80, 150, "expanded", "analysis", None))
        self.assertEqual(regions[2], CodeRegion("region-151-1000", 151, 1000, "collapsed", "collapsed"))
        self.assertContinuousCoverage(regions, 1000)

    # Case 11: Fallback range overlaps with function range
    def test_case_11_fallback_overlaps_function_range(self):
        # Function: 200..300
        # Fallback line 185 -> 165..205 (overlaps 200..300)
        # Merged -> 165..300
        functions = [FunctionRange(200, 300, "worker()")]
        regions = build_code_regions(
            total_lines=1000, pending_lines=[185, 250], function_ranges=functions
        )
        self.assertEqual(len(regions), 3)
        self.assertEqual(regions[0], CodeRegion("region-1-164", 1, 164, "collapsed", "collapsed"))
        self.assertEqual(regions[1].start_line, 165)
        self.assertEqual(regions[1].end_line, 300)
        self.assertEqual(regions[1].default_state, "expanded")
        self.assertEqual(regions[1].label, "worker()")
        self.assertEqual(regions[2], CodeRegion("region-301-1000", 301, 1000, "collapsed", "collapsed"))
        self.assertContinuousCoverage(regions, 1000)

    # Case 12: Very large function (> 5000 lines) -> whole function expanded
    def test_case_12_very_large_function(self):
        functions = [FunctionRange(200, 5200, "huge_dispatch()")]
        regions = build_code_regions(
            total_lines=10000, pending_lines=[866], function_ranges=functions
        )
        self.assertEqual(len(regions), 3)
        self.assertEqual(regions[1], CodeRegion("region-200-5200", 200, 5200, "expanded", "analysis", "huge_dispatch()"))
        self.assertEqual(regions[1].line_count, 5001)
        self.assertContinuousCoverage(regions, 10000)

    # Case 13: Invalid function ranges handled gracefully
    def test_case_13_invalid_function_ranges_ignored(self):
        invalid_functions = [
            FunctionRange(-10, 50, "neg()"),
            FunctionRange(300, 200, "backwards()"),
            FunctionRange(0, 0, "zero()"),
            FunctionRange(100, 250, "valid_fn()"),
        ]
        regions = build_code_regions(
            total_lines=1000, pending_lines=[150], function_ranges=invalid_functions
        )
        self.assertEqual(len(regions), 3)
        self.assertEqual(regions[1], CodeRegion("region-100-250", 100, 250, "expanded", "analysis", "valid_fn()"))
        self.assertContinuousCoverage(regions, 1000)

    # Case 14: Comprehensive verification of continuous coverage
    def test_case_14_complex_continuous_coverage(self):
        functions = [
            FunctionRange(10, 50, "fn1()"),
            FunctionRange(65, 80, "fn2()"),  # gap = 65 - 50 - 1 = 14 -> merge with fn1 (10..80)
            FunctionRange(200, 400, "fn3()"),
            FunctionRange(700, 900, "fn4()"),
        ]
        # Pending lines in functions + unmapped lines
        pending = [25, 75, 150, 300, 950]
        regions = build_code_regions(
            total_lines=1000, pending_lines=pending, function_ranges=functions
        )
        self.assertContinuousCoverage(regions, 1000)

    def test_find_function_binary_search(self):
        fns = sanitize_function_ranges([
            FunctionRange(10, 50, "a()"),
            FunctionRange(60, 100, "b()"),
            FunctionRange(200, 260, "c()"),
            FunctionRange(270, 300, "d()"),
        ])
        self.assertEqual(find_function_containing_line(5, fns), None)
        self.assertEqual(find_function_containing_line(10, fns).name, "a()")
        self.assertEqual(find_function_containing_line(30, fns).name, "a()")
        self.assertEqual(find_function_containing_line(50, fns).name, "a()")
        self.assertEqual(find_function_containing_line(55, fns), None)
        self.assertEqual(find_function_containing_line(230, fns).name, "c()")
        self.assertEqual(find_function_containing_line(280, fns).name, "d()")


if __name__ == "__main__":
    unittest.main()
