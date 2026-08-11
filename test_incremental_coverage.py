#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for Git incremental coverage calculation and fillable review generation."""

import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
import zipfile

try:
    import ast
except ImportError:
    ast = None

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

    @unittest.mock.patch("coverage_check.subprocess.Popen")
    def test_git_diff_uses_python36_compatible_subprocess_api(self, mock_popen):
        process = mock_popen.return_value
        process.communicate.return_value = (b"diff output", b"")
        process.returncode = 0

        self.assertEqual(
            coverage_check.run_git_diff(self.repo_dir, "old", "new"),
            "diff output",
        )
        _, kwargs = mock_popen.call_args
        self.assertNotIn("text", kwargs)
        self.assertNotIn("encoding", kwargs)

    @unittest.mock.patch("coverage_check.run_git_diff")
    def test_calculate_multiple_repositories_uses_absolute_lcov_paths(self, mock_git_diff):
        repo_b = os.path.join(self.temp_dir, "repo_b")
        repo_c = os.path.join(self.temp_dir, "repo_c")
        os.makedirs(repo_b)
        os.makedirs(repo_c)
        with open(self.info_path, "w", encoding="utf-8") as info_file:
            for repo_path in (self.repo_dir, repo_b, repo_c):
                info_file.write("SF:{}/src/shared.c\nDA:10,1\nDA:11,0\nend_of_record\n".format(repo_path))

        repositories = [
            {"name": "repo_a", "path": self.repo_dir, "oldgit": "a1", "newgit": "a2"},
            {"name": "repo_b", "path": repo_b, "oldgit": "b1", "newgit": "b2"},
            {"name": "repo_c", "path": repo_c, "oldgit": "c1", "newgit": "c2"},
        ]
        mock_git_diff.return_value = """--- a/src/shared.c
+++ b/src/shared.c
@@ -9,0 +10,2 @@
+covered();
+uncovered();
"""

        result = coverage_check.calculate_multi_repo_incremental_coverage(repositories, self.info_path)

        self.assertEqual(result["summary"]["changed_lines"], 6)
        self.assertEqual(result["summary"]["covered"], 3)
        self.assertEqual(result["summary"]["uncovered"], 3)
        self.assertEqual(result["summary"]["coverage_rate"], 50.0)
        self.assertEqual(set(item["repository"] for item in result["details"]), {"repo_a", "repo_b", "repo_c"})
        self.assertEqual(
            result["review_lines_by_file"],
            {
                os.path.join(self.repo_dir, "src", "shared.c"): [11],
                os.path.join(repo_b, "src", "shared.c"): [11],
                os.path.join(repo_c, "src", "shared.c"): [11],
            },
        )

        xlsx_path = os.path.join(self.temp_dir, "multi.xlsx")
        coverage_check.write_result_excel(result, xlsx_path)
        with zipfile.ZipFile(xlsx_path) as workbook:
            self.assertIn("xl/worksheets/sheet3.xml", workbook.namelist())

    @unittest.mock.patch("coverage_check.run_git_diff")
    def test_repositories_config_resolves_relative_repository_paths(self, mock_git_diff):
        config_path = os.path.join(self.temp_dir, "repositories.json")
        with open(config_path, "w", encoding="utf-8") as config_file:
            json.dump({"repositories": [{
                "name": "repo_a", "path": "repo", "oldgit": "a1", "newgit": "a2",
            }]}, config_file)
        mock_git_diff.return_value = ""

        result = coverage_check.calculate_multi_repo_incremental_coverage_from_config(config_path, self.info_path)

        self.assertEqual(result["repositories"][0]["path"], self.repo_dir)

    @unittest.mock.patch("coverage_check.run_git_diff")
    def test_multi_repository_rejects_relative_lcov_source_paths(self, mock_git_diff):
        repo_b = os.path.join(self.temp_dir, "repo_b")
        os.makedirs(repo_b)
        with open(self.info_path, "w", encoding="utf-8") as info_file:
            info_file.write("SF:src/shared.c\nDA:10,0\nend_of_record\n")
        mock_git_diff.return_value = """--- a/src/shared.c
+++ b/src/shared.c
@@ -9,0 +10 @@
+uncovered();
"""

        with self.assertRaises(ValueError):
            coverage_check.calculate_multi_repo_incremental_coverage([
                {"name": "repo_a", "path": self.repo_dir, "oldgit": "a1", "newgit": "a2"},
                {"name": "repo_b", "path": repo_b, "oldgit": "b1", "newgit": "b2"},
            ], self.info_path)


class TestPython36Compatibility(unittest.TestCase):
    def test_project_scripts_use_python36_grammar(self):
        project_dir = os.path.dirname(os.path.abspath(__file__))
        script_paths = sorted(
            os.path.join(project_dir, filename)
            for filename in os.listdir(project_dir)
            if filename.endswith(".py")
        )
        for script_path in script_paths:
            with open(script_path, "r", encoding="utf-8") as script_file:
                source = script_file.read()
            if ast is not None and sys.version_info >= (3, 8):
                ast.parse(source, filename=script_path, feature_version=6)
            else:
                compile(source, script_path, "exec")

    def test_project_does_not_use_unconditional_python37_apis(self):
        project_dir = os.path.dirname(os.path.abspath(__file__))
        forbidden_fragments = (
            "subprocess.run(",
            "asyncio.run(",
            "breakpoint(",
            "import dataclasses",
            "from dataclasses import",
            "import contextvars",
            "from contextvars import",
            "time.time_ns(",
            ".fromisoformat(",
        )
        for filename in os.listdir(project_dir):
            if not filename.endswith(".py") or filename.startswith("test_"):
                continue
            script_path = os.path.join(project_dir, filename)
            with open(script_path, "r", encoding="utf-8") as script_file:
                source = script_file.read()
            for fragment in forbidden_fragments:
                self.assertNotIn(
                    fragment,
                    source,
                    "{} requires Python 3.7+ API: {}".format(filename, fragment),
                )

    def test_threading_http_server_has_python36_fallback(self):
        project_dir = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(project_dir, "enhance_coverage.py"), "r", encoding="utf-8") as script_file:
            source = script_file.read()
        self.assertIn("from socketserver import ThreadingMixIn", source)
        self.assertIn("class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):", source)


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
<span class="lineNum"> 11 </span><span class="branchNoCov"> - </span><span class="lineNoCov"> selected();</span>
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
        self.assertIn(
            '<span class="lineNoCov" data-coverage-review="incremental"> selected();</span>',
            enhanced,
        )
        self.assertNotIn(
            'class="branchNoCov" data-coverage-review="incremental"',
            enhanced,
        )
        with open(os.path.join(self.output_dir, "coverage_enhance.js"), "r", encoding="utf-8") as js_file:
            self.assertIn('const REVIEW_SCOPE = "incremental";', js_file.read())
        with open(os.path.join(self.output_dir, "coverage_progress.html"), "r", encoding="utf-8") as progress_file:
            self.assertIn('const DEFAULT_REVIEW_SCOPE = "incremental";', progress_file.read())
        self.assertTrue(os.path.exists(os.path.join(self.output_dir, "incremental_coverage.html")))
        self.assertTrue(os.path.exists(os.path.join(self.output_dir, "incremental_coverage.xlsx")))
        with open(os.path.join(self.output_dir, "incremental_coverage.json"), "r", encoding="utf-8") as result_file:
            self.assertEqual(json.load(result_file)["review_scope"], "incremental")


class TestMultiRepositoryReviewInjection(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.input_dir = os.path.join(self.temp_dir, "coverage")
        self.output_dir = os.path.join(self.temp_dir, "review")
        self.repo_a_source = os.path.join(self.temp_dir, "repo_a", "src", "shared.c")
        self.repo_b_source = os.path.join(self.temp_dir, "repo_b", "src", "shared.c")
        os.makedirs(self.input_dir)
        for filename, source_path in (("repo_a.gcov.html", self.repo_a_source), ("repo_b.gcov.html", self.repo_b_source)):
            with open(os.path.join(self.input_dir, filename), "w", encoding="utf-8") as page:
                page.write("""<!doctype html><html><head><title>LCOV - cov - {}</title></head>
<body><pre class="source"><span class="lineNum"> 11 </span><span class="lineNoCov"> target();</span></pre></body></html>""".format(source_path))

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    @unittest.mock.patch("enhance_coverage.DatabaseManager")
    def test_multi_repo_result_marks_each_absolute_source_page(self, mock_db_manager):
        mock_db_manager.return_value = unittest.mock.MagicMock()
        result = {
            "generated_at": "2026-08-11 10:00:00",
            "oldgit": "multiple",
            "newgit": "multiple",
            "summary": {
                "changed_lines": 2, "covered": 0, "uncovered": 2, "ignored": 0,
                "missing": 0, "coverable_total": 2, "coverage_rate": 0.0,
            },
            "repositories": [
                {"name": "repo_a", "path": os.path.dirname(os.path.dirname(self.repo_a_source)),
                 "oldgit": "a1", "newgit": "a2", "summary": {"changed_lines": 1, "covered": 0, "uncovered": 1, "ignored": 0, "missing": 0, "coverable_total": 1, "coverage_rate": 0.0}},
                {"name": "repo_b", "path": os.path.dirname(os.path.dirname(self.repo_b_source)),
                 "oldgit": "b1", "newgit": "b2", "summary": {"changed_lines": 1, "covered": 0, "uncovered": 1, "ignored": 0, "missing": 0, "coverable_total": 1, "coverage_rate": 0.0}},
            ],
            "details": [
                {"repository": "repo_a", "file_path": "src/shared.c", "coverage_file": self.repo_a_source,
                 "review_file_path": self.repo_a_source, "line_number": 11, "execution_count": 0,
                 "status": coverage_check.STATUS_UNCOVERED},
                {"repository": "repo_b", "file_path": "src/shared.c", "coverage_file": self.repo_b_source,
                 "review_file_path": self.repo_b_source, "line_number": 11, "execution_count": 0,
                 "status": coverage_check.STATUS_UNCOVERED},
            ],
            "uncovered_lines_by_file": {"repo_a:src/shared.c": [11], "repo_b:src/shared.c": [11]},
            "review_lines_by_file": {self.repo_a_source: [11], self.repo_b_source: [11]},
        }

        enhance_coverage.build_incremental_review_site(
            result, self.input_dir, self.output_dir, "multi_review", workers=1, render_mode="lazy"
        )

        for filename in ("repo_a.gcov.html", "repo_b.gcov.html"):
            with open(os.path.join(self.output_dir, filename), "r", encoding="utf-8") as page:
                self.assertIn('data-coverage-review="incremental"', page.read())
        with open(os.path.join(self.output_dir, "incremental_coverage.html"), "r", encoding="utf-8") as summary_page:
            summary_html = summary_page.read()
        self.assertIn("2 个仓库的 Git 范围", summary_html)
        self.assertIn("repo_a", summary_html)
        self.assertIn("repo_b", summary_html)
        with zipfile.ZipFile(os.path.join(self.output_dir, "incremental_coverage.xlsx")) as workbook:
            self.assertIn("xl/worksheets/sheet3.xml", workbook.namelist())


if __name__ == "__main__":
    unittest.main()
