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

    def test_file_extension_filtering(self):
        self.assertTrue(coverage_check.is_valid_source_file("src/main.c"))
        self.assertTrue(coverage_check.is_valid_source_file("include/header.h"))
        self.assertFalse(coverage_check.is_valid_source_file("Makefile"))
        self.assertFalse(coverage_check.is_valid_source_file("config.json"))
        self.assertFalse(coverage_check.is_valid_source_file("libfoo.so"))

        diff_text = """diff --git a/src/main.c b/src/main.c
+++ b/src/main.c
@@ -10,0 +10,1 @@
+covered();
diff --git a/Makefile b/Makefile
+++ b/Makefile
@@ -1,0 +1,5 @@
+ALL: target
diff --git a/config.json b/config.json
+++ b/config.json
@@ -1,0 +1,5 @@
+{"key": "value"}
"""
        file_changes = coverage_check.parse_diff_text(diff_text)
        self.assertIn("src/main.c", file_changes)
        self.assertNotIn("Makefile", file_changes)
        self.assertNotIn("config.json", file_changes)

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
            self.repo_dir, "old", "new", self.info_path, diff_text,
            developer_file_changes=[],
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
            self.assertIn("xl/worksheets/sheet3.xml", workbook.namelist())
            self.assertIn("Developer Summary", workbook.read("xl/workbook.xml").decode("utf-8"))

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

    @unittest.mock.patch("coverage_check.subprocess.Popen")
    def test_git_log_collects_author_and_committed_files(self, mock_popen):
        process = mock_popen.return_value
        process.communicate.return_value = (
            b"abc1234\x1fAlice\x1falice@example.com\x1f2026-08-13T10:00:00+08:00\x1fadd coverage\n"
            b"M\tsrc/main.c\nR100\tsrc/old.c\tsrc/new.c\nD\tsrc/deleted.c\n",
            b"",
        )
        process.returncode = 0

        changes = coverage_check.run_git_developer_file_changes(
            self.repo_dir, "old", "new", "platform"
        )

        self.assertEqual([item["file_path"] for item in changes], ["src/main.c", "src/new.c"])
        self.assertTrue(all(item["author_email"] == "alice@example.com" for item in changes))
        self.assertTrue(all(item["repository"] == "platform" for item in changes))
        self.assertIn("--name-status", mock_popen.call_args[0][0])

    def test_developer_tasks_show_each_author_their_jointly_changed_file(self):
        details = [
            {"repository": "platform", "file_path": "src/main.c", "review_file_path": "/repo/src/main.c",
             "status": coverage_check.STATUS_UNCOVERED},
            {"repository": "platform", "file_path": "src/main.c", "review_file_path": "/repo/src/main.c",
             "status": coverage_check.STATUS_UNCOVERED},
            {"repository": "platform", "file_path": "src/main.c", "review_file_path": "/repo/src/main.c",
             "status": coverage_check.STATUS_COVERED},
            {"repository": "platform", "file_path": "docs/readme.md", "review_file_path": "/repo/docs/readme.md",
             "status": coverage_check.STATUS_IGNORED},
        ]
        changes = [
            {"repository": "platform", "commit": "a1", "author_name": "Alice", "author_email": "alice@example.com",
             "committed_at": "2026-08-13T10:00:00+08:00", "subject": "main", "file_path": "src/main.c", "change_type": "M"},
            {"repository": "platform", "commit": "b1", "author_name": "Bob", "author_email": "bob@example.com",
             "committed_at": "2026-08-13T11:00:00+08:00", "subject": "joint", "file_path": "src/main.c", "change_type": "M"},
            {"repository": "platform", "commit": "b1", "author_name": "Bob", "author_email": "bob@example.com",
             "committed_at": "2026-08-13T11:00:00+08:00", "subject": "docs", "file_path": "docs/readme.md", "change_type": "A"},
        ]

        tasks = coverage_check.build_developer_tasks(details, changes)["developers"]
        alice = next(item for item in tasks if item["email"] == "alice@example.com")
        bob = next(item for item in tasks if item["email"] == "bob@example.com")
        self.assertEqual(alice["review_file_total"], 1)
        self.assertEqual(alice["review_uncovered_total"], 2)
        self.assertEqual(bob["changed_file_total"], 2)
        self.assertEqual(bob["files"][0]["file_path"], "src/main.c")

    @unittest.mock.patch("coverage_check.run_git_developer_file_changes", return_value=[])
    @unittest.mock.patch("coverage_check.run_git_diff")
    def test_calculate_multiple_repositories_uses_absolute_lcov_paths(self, mock_git_diff, mock_developer_changes):
        repo_b = os.path.join(self.temp_dir, "repo_b")
        repo_c = os.path.join(self.temp_dir, "repo_c")
        os.makedirs(repo_b)
        os.makedirs(repo_c)
        with open(self.info_path, "w", encoding="utf-8") as info_file:
            for repo_path in (self.repo_dir, repo_b, repo_c):
                info_file.write("SF:{}/src/shared.c\nDA:10,1\nDA:11,0\nend_of_record\n".format(coverage_check.normalize_path(repo_path)))

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
                coverage_check.normalize_path(os.path.join(self.repo_dir, "src", "shared.c")): [11],
                coverage_check.normalize_path(os.path.join(repo_b, "src", "shared.c")): [11],
                coverage_check.normalize_path(os.path.join(repo_c, "src", "shared.c")): [11],
            },
        )

        xlsx_path = os.path.join(self.temp_dir, "multi.xlsx")
        coverage_check.write_result_excel(result, xlsx_path)
        with zipfile.ZipFile(xlsx_path) as workbook:
            self.assertIn("xl/worksheets/sheet3.xml", workbook.namelist())

    @unittest.mock.patch("coverage_check.run_git_developer_file_changes", return_value=[])
    @unittest.mock.patch("coverage_check.run_git_diff")
    def test_repositories_config_resolves_relative_repository_paths(self, mock_git_diff, mock_developer_changes):
        config_path = os.path.join(self.temp_dir, "repositories.json")
        with open(config_path, "w", encoding="utf-8") as config_file:
            json.dump({"repositories": [{
                "name": "repo_a", "path": "repo", "oldgit": "a1", "newgit": "a2",
            }]}, config_file)
        mock_git_diff.return_value = ""

        result = coverage_check.calculate_multi_repo_incremental_coverage_from_config(config_path, self.info_path)

        self.assertEqual(result["repositories"][0]["path"], self.repo_dir)

    @unittest.mock.patch("coverage_check.run_git_developer_file_changes", return_value=[])
    @unittest.mock.patch("coverage_check.run_git_diff")
    def test_multi_repository_rejects_relative_lcov_source_paths(self, mock_git_diff, mock_developer_changes):
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
        self.page_path = os.path.join(self.input_dir, "module.c.gcov.html")
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

        with open(os.path.join(self.output_dir, "module.c.gcov.html"), "r", encoding="utf-8") as page:
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
            self.assertIn('data-review-scope="incremental"', progress_file.read())
        with open(os.path.join(self.output_dir, "coverage_progress.js"), "r", encoding="utf-8") as progress_js_file:
            self.assertIn('const DEFAULT_REVIEW_SCOPE = "incremental";', progress_js_file.read())
        self.assertTrue(os.path.exists(os.path.join(self.output_dir, "incremental_coverage.html")))
        self.assertTrue(os.path.exists(os.path.join(self.output_dir, "incremental_developer_tasks.html")))
        self.assertTrue(os.path.exists(os.path.join(self.output_dir, "incremental_coverage.xlsx")))
        with open(os.path.join(self.output_dir, "incremental_coverage.json"), "r", encoding="utf-8") as result_file:
            self.assertEqual(json.load(result_file)["review_scope"], "incremental")


class TestMultiRepositoryReviewInjection(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.input_dir = os.path.join(self.temp_dir, "coverage")
        self.output_dir = os.path.join(self.temp_dir, "review")
        self.repo_a_source = coverage_check.normalize_path(os.path.join(self.temp_dir, "repo_a", "src", "shared.c"))
        self.repo_b_source = coverage_check.normalize_path(os.path.join(self.temp_dir, "repo_b", "src", "shared.c"))
        os.makedirs(self.input_dir)
        for filename, source_path in (("repo_a.c.gcov.html", self.repo_a_source), ("repo_b.c.gcov.html", self.repo_b_source)):
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

        for filename in ("repo_a.c.gcov.html", "repo_b.c.gcov.html"):
            with open(os.path.join(self.output_dir, filename), "r", encoding="utf-8") as page:
                self.assertIn('data-coverage-review="incremental"', page.read())
        with open(os.path.join(self.output_dir, "incremental_coverage.html"), "r", encoding="utf-8") as summary_page:
            summary_html = summary_page.read()
        self.assertIn("2 个仓库的 Git 范围", summary_html)
        self.assertIn("repo_a", summary_html)
        self.assertIn("repo_b", summary_html)
        self.assertIn('data-sort-key="uncovered"', summary_html)
        self.assertIn('<script src="incremental_coverage.js?v=', summary_html)
        with open(os.path.join(self.output_dir, "incremental_coverage.js"), "r", encoding="utf-8") as summary_js:
            self.assertIn('sortRows("uncovered", -1)', summary_js.read())
        self.assertIn('data-sort-value="1"', summary_html)
        with zipfile.ZipFile(os.path.join(self.output_dir, "incremental_coverage.xlsx")) as workbook:
            self.assertIn("xl/worksheets/sheet3.xml", workbook.namelist())

    def test_summary_defaults_to_most_uncovered_file_first(self):
        os.makedirs(self.output_dir)
        result = {
            "generated_at": "2026-08-11 12:00:00",
            "oldgit": "old123",
            "newgit": "new456",
            "summary": {
                "changed_lines": 3, "covered": 0, "uncovered": 3, "ignored": 0,
                "missing": 0, "coverable_total": 3, "coverage_rate": 0.0,
            },
            "details": [
                {"file_path": "src/low.c", "line_number": 10,
                 "execution_count": 0, "status": coverage_check.STATUS_UNCOVERED},
                {"file_path": "src/high.c", "line_number": 10,
                 "execution_count": 0, "status": coverage_check.STATUS_UNCOVERED},
                {"file_path": "src/high.c", "line_number": 11,
                 "execution_count": 0, "status": coverage_check.STATUS_UNCOVERED},
            ],
        }

        enhance_coverage.write_incremental_summary_page(self.output_dir, "sort_test", result)

        with open(os.path.join(self.output_dir, "incremental_coverage.html"), "r", encoding="utf-8") as summary_page:
            summary_html = summary_page.read()
        self.assertLess(summary_html.index("src/high.c"), summary_html.index("src/low.c"))
        self.assertIn('data-sort-key="changed"', summary_html)
        self.assertIn('data-sort-key="covered"', summary_html)
        self.assertIn('data-sort-key="uncovered"', summary_html)

    @unittest.mock.patch("enhance_coverage.load_ownership_workbook")
    def test_summary_shows_team_and_leader_between_repository_and_file(self, mock_workbook):
        os.makedirs(self.output_dir)
        rule = {
            "module": "NETWORK", "module_key": "NETWORK",
            "segments": ("src", "network"),
        }
        mock_workbook.return_value = {
            "available": True,
            "suffix_rules": {("src", "network"): rule},
            "owner_rules": {"NETWORK": {"team": "网络平台组", "leader": "王工"}},
        }
        result = {
            "generated_at": "2026-08-13 12:00:00",
            "oldgit": "old123", "newgit": "new456",
            "summary": {
                "changed_lines": 1, "covered": 0, "uncovered": 1, "ignored": 0,
                "missing": 0, "coverable_total": 1, "coverage_rate": 0.0,
            },
            "details": [{
                "repository": "platform", "file_path": "src/network/main.c",
                "review_file_path": "/build/repo/src/network/main.c", "line_number": 10,
                "execution_count": 0, "status": coverage_check.STATUS_UNCOVERED,
            }],
        }

        enhance_coverage.write_incremental_summary_page(
            self.output_dir, "ownership_test", result
        )

        with open(os.path.join(self.output_dir, "incremental_coverage.html"), "r", encoding="utf-8") as summary_page:
            summary_html = summary_page.read()
        self.assertIn("网络平台组 / 王工", summary_html)
        self.assertIn('data-sort-key="module"', summary_html)
        self.assertIn('data-sort-key="team"', summary_html)
        self.assertIn('data-sort-key="leader"', summary_html)
        repository_header = summary_html.index('data-sort-key="repository"')
        team_header = summary_html.index('data-sort-key="team"')
        leader_header = summary_html.index('data-sort-key="leader"')
        module_header = summary_html.index('data-sort-key="module"')
        file_header = summary_html.index('data-sort-key="file"')
        self.assertLess(repository_header, team_header)
        self.assertLess(team_header, leader_header)
        self.assertLess(leader_header, module_header)
        self.assertLess(module_header, file_header)

    def test_developer_task_page_lists_files_and_pending_fill(self):
        os.makedirs(self.output_dir)
        result = {
            "generated_at": "2026-08-13 12:00:00",
            "oldgit": "old123",
            "newgit": "new456",
            "summary": {
                "changed_lines": 2, "covered": 0, "uncovered": 2, "ignored": 0,
                "missing": 0, "coverable_total": 2, "coverage_rate": 0.0,
            },
            "details": [],
            "developer_tasks": {"developers": [{
                "name": "Alice", "email": "alice@example.com", "commit_total": 1,
                "changed_file_total": 1, "review_file_total": 1, "review_uncovered_total": 2,
                "files": [{
                    "repository": "platform", "file_path": "src/main.c",
                    "review_file_path": "src/main.c", "change_types": ["M"],
                    "commits": [{"commit": "abcdef123456", "subject": "add main", "committed_at": "2026-08-13T12:00:00+08:00"}],
                    "changed": 2, "covered": 0, "uncovered": 2, "ignored": 0, "missing": 0,
                }],
            }]},
        }

        enhance_coverage.write_incremental_developer_tasks_page(
            self.output_dir, "developer_test", result
        )

        with open(os.path.join(self.output_dir, "incremental_developer_tasks.html"), "r", encoding="utf-8") as page:
            page_html = page.read()
        self.assertIn("Alice", page_html)
        self.assertIn("src/main.c", page_html)
        self.assertIn("待填写 2 行", page_html)
        self.assertIn("多人提交同一文件", page_html)

    def test_url_search_param_parsing_and_history_replace_state_in_incremental_js(self):
        js_path = enhance_coverage.INCREMENTAL_JS_SOURCE_PATH
        with open(js_path, "r", encoding="utf-8") as f:
            js_content = f.read()

        self.assertIn('URLSearchParams(window.location.search)', js_content)
        self.assertIn('params.get("repo")', js_content)
        self.assertIn('params.get("module")', js_content)
        self.assertIn('params.get("team")', js_content)
        self.assertIn('params.get("leader")', js_content)
        self.assertIn('window.history.replaceState', js_content)

    def test_coverage_progress_js_dynamic_review_scope_urls_and_styles(self):
        js_path = enhance_coverage.PROGRESS_JS_SOURCE_PATH
        with open(js_path, "r", encoding="utf-8") as f:
            js_content = f.read()

        self.assertIn('const isIncremental = reviewScope === \'incremental\';', js_content)
        self.assertIn('<span class="mod-chip">', js_content)
        self.assertIn('PROGRESS_PAGE_VERSION = \'visible-progress-20260817_v9_7\'', js_content)

        html_path = enhance_coverage.PROGRESS_PAGE_SOURCE_PATH
        with open(html_path, "r", encoding="utf-8") as f:
            html_content = f.read()

        self.assertIn('.progress-link', html_content)
        self.assertIn('.progress-link:hover', html_content)
        self.assertIn('页面版本 visible-progress-20260817_v9_7', html_content)

    def test_cascading_filter_dropdowns_in_incremental_js(self):
        js_path = enhance_coverage.INCREMENTAL_JS_SOURCE_PATH
        with open(js_path, "r", encoding="utf-8") as f:
            js_content = f.read()

        self.assertIn('function updateSelectOptions', js_content)
        self.assertIn('function updateFilterDropdowns', js_content)
        self.assertIn('updateSelectOptions(repoFilter, validRepos, curRepo);', js_content)
        self.assertIn('updateSelectOptions(teamFilter, validTeams, curTeam);', js_content)
        self.assertIn('updateSelectOptions(leaderFilter, validLeaders, curLeader);', js_content)
        self.assertIn('updateSelectOptions(moduleFilter, validModules, curModule);', js_content)
        self.assertIn('if (fileSearch) fileSearch.addEventListener("input", debouncedApplyFilters);', js_content)

    def test_search_keyword_decoupled_from_dropdown_candidates(self):
        js_path = enhance_coverage.INCREMENTAL_JS_SOURCE_PATH
        with open(js_path, "r", encoding="utf-8") as f:
            js_content = f.read()

        # Ensure updateFilterDropdowns does not use kw to filter validRepos or validTeams
        func_start = js_content.find('function updateFilterDropdowns()')
        func_end = js_content.find('function applyFilters()')
        dropdown_func = js_content[func_start:func_end]
        self.assertNotIn('fullText.indexOf(kw)', dropdown_func)
        self.assertNotIn('var kw = fileSearch', dropdown_func)

    def test_sync_incremental_unanalyzed_counts_consistency(self):
        sample_result = {
            "summary": {"uncovered": 10, "unanalyzed": 10},
            "details": [
                {"repository": "ssf", "file_path": "a.c", "status": coverage_check.STATUS_UNCOVERED},
                {"repository": "ssf", "file_path": "b.c", "status": coverage_check.STATUS_UNCOVERED},
            ]
        }
        res = enhance_coverage.sync_incremental_unanalyzed_counts("test_project", sample_result)
        self.assertEqual(sample_result["summary"]["unanalyzed"], 2)


if __name__ == "__main__":
    unittest.main()
