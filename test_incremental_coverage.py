#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for Git incremental coverage calculation and fillable review generation."""

import json
import os
import shutil
import tempfile
import unittest
import unittest.mock
import zipfile

import coverage_check
import enhance_coverage


class TestIncrementalCoverageCalculation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.repo_dir = os.path.join(self.temp_dir, "repo")
        os.makedirs(os.path.join(self.repo_dir, "src"))
        self.info_path = os.path.join(self.temp_dir, "coverage.info")
        with open(self.info_path, "w", encoding="utf-8") as info_file:
            info_file.write("""TN:
SF:{repo}/src/main.c
DA:10,3
DA:11,0
end_of_record
""".format(repo=self.repo_dir))

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_calculate_added_line_statuses_and_xlsx(self):
        diff_text = """diff --git a/src/main.c b/src/main.c
index 111..222 100644
--- a/src/main.c
+++ b/src/main.c
@@ -8,0 +10,3 @@
+covered();
+uncovered();
+comment_only();
diff --git a/src/not_in_lcov.c b/src/not_in_lcov.c
--- a/src/not_in_lcov.c
+++ b/src/not_in_lcov.c
@@ -0,0 +1 @@
+missing();
"""
        result = coverage_check.calculate_incremental_coverage(
            self.repo_dir, "old", "new", self.info_path, diff_text
        )

        self.assertEqual(result["summary"]["changed_lines"], 4)
        self.assertEqual(result["summary"]["covered"], 1)
        self.assertEqual(result["summary"]["uncovered"], 1)
        self.assertEqual(result["summary"]["ignored"], 1)
        self.assertEqual(result["summary"]["missing"], 1)
        self.assertEqual(result["uncovered_lines_by_file"], {"src/main.c": [11]})
        self.assertEqual(result["summary"]["coverage_rate"], 50.0)

        xlsx_path = os.path.join(self.temp_dir, "incremental.xlsx")
        coverage_check.write_result_excel(result, xlsx_path)
        with zipfile.ZipFile(xlsx_path) as workbook:
            self.assertIn("xl/worksheets/sheet1.xml", workbook.namelist())
            self.assertIn("xl/worksheets/sheet2.xml", workbook.namelist())


class TestIncrementalReviewInjection(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.repo_dir = os.path.join(self.temp_dir, "repo")
        self.input_dir = os.path.join(self.temp_dir, "coverage")
        self.output_dir = os.path.join(self.temp_dir, "review")
        os.makedirs(self.repo_dir)
        os.makedirs(self.input_dir)
        self.page_path = os.path.join(self.input_dir, "module.gcov.html")
        with open(self.page_path, "w", encoding="utf-8") as page:
            page.write("""<!doctype html><html><head><title>LCOV - cov - src/main.c</title></head>
<body><pre class="source">
<span class="lineNum"> 10 </span><span class="lineCov"> covered();</span>
<span class="lineNum"> 11 </span><span class="lineNoCov"> selected();</span>
<span class="lineNum"> 12 </span><span class="lineNoCov"> existing_uncovered();</span>
</pre></body></html>""")

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    @unittest.mock.patch("enhance_coverage.DatabaseManager")
    @unittest.mock.patch("enhance_coverage.coverage_check.calculate_incremental_coverage")
    def test_review_page_only_marks_new_uncovered_lines(self, mock_calculate, mock_db_manager):
        mock_db_manager.return_value = unittest.mock.MagicMock()
        mock_calculate.return_value = {
            "generated_at": "2026-08-10 12:00:00",
            "repo_path": self.repo_dir,
            "oldgit": "old123",
            "newgit": "new456",
            "info_files": [],
            "summary": {
                "changed_lines": 2, "covered": 1, "uncovered": 1, "ignored": 0,
                "missing": 0, "coverable_total": 2, "coverage_rate": 50.0,
            },
            "details": [
                {"file_path": "src/main.c", "coverage_file": "src/main.c", "line_number": 10,
                 "execution_count": 1, "status": coverage_check.STATUS_COVERED},
                {"file_path": "src/main.c", "coverage_file": "src/main.c", "line_number": 11,
                 "execution_count": 0, "status": coverage_check.STATUS_UNCOVERED},
            ],
            "uncovered_lines_by_file": {"src/main.c": [11]},
        }

        enhance_coverage.generate_incremental_review(
            self.repo_dir, "old123", "new456", "coverage.info", self.input_dir,
            self.output_dir, "incremental_test", workers=1, render_mode="lazy",
        )

        with open(os.path.join(self.output_dir, "module.gcov.html"), "r", encoding="utf-8") as page:
            enhanced = page.read()
        self.assertIn('data-coverage-review="incremental"', enhanced)
        self.assertEqual(enhanced.count('data-coverage-review="incremental"'), 1)
        with open(os.path.join(self.output_dir, "coverage_enhance.js"), "r", encoding="utf-8") as js_file:
            self.assertIn('const REVIEW_SCOPE = "incremental";', js_file.read())
        with open(os.path.join(self.output_dir, "coverage_progress.html"), "r", encoding="utf-8") as progress_file:
            self.assertIn('const DEFAULT_REVIEW_SCOPE = "incremental";', progress_file.read())
        self.assertTrue(os.path.exists(os.path.join(self.output_dir, "incremental_coverage.html")))
        self.assertTrue(os.path.exists(os.path.join(self.output_dir, "incremental_coverage.xlsx")))
        with open(os.path.join(self.output_dir, "incremental_coverage.json"), "r", encoding="utf-8") as result_file:
            self.assertEqual(json.load(result_file)["review_scope"], "incremental")


if __name__ == "__main__":
    unittest.main()
