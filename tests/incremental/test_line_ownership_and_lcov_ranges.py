"""Targeted verification for line ownership and LCOV function-range reuse."""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import coverage_check
import enhance_coverage
from app.code_detail.sidecar_store import SidecarStore
from app.incremental.service import IncrementalService
from app.inject.parse_once import parse_gcov_source_once
from code_region import build_code_regions
from code_detail_service import CodeDetailService
from code_region import FunctionRange
from source_reader import SourceLineDTO, parse_source_lines_from_gcov_html


class TestLineOwnershipAndLCOVRanges(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_info(self, content):
        path = os.path.join(self.temp_dir, "coverage.info")
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(content)
        return path

    def test_blame_porcelain_parses_metadata_boundary_and_selected_lines(self):
        text = """^abcdef0123456789abcdef0123456789abcdef01 1 10 2
author Alice Example
author-mail <alice@example.com>
author-time 1700000000
author-tz +0800
summary first subject
filename src/a.c
\tline ten
\tline eleven
def5678 3 12
author Bob
author-mail <bob@example.com>
author-time 1700000001
author-tz +0000
summary second subject
filename src/a.c
\tline twelve
"""
        parsed = coverage_check.parse_git_blame_porcelain(text, {10, 11, 12})
        self.assertEqual(parsed[10]["author_name"], "Alice Example")
        self.assertEqual(parsed[10]["author_email"], "alice@example.com")
        self.assertEqual(parsed[10]["commit"], "abcdef0123456789abcdef0123456789abcdef01")
        self.assertTrue(parsed[10]["boundary"])
        self.assertEqual(parsed[11]["commit"], "abcdef0123456789abcdef0123456789abcdef01")
        self.assertEqual(parsed[12]["author_name"], "Bob")
        self.assertEqual(parsed[12]["subject"], "second subject")

    def test_blame_porcelain_accepts_blank_boundary_sha_from_git_b(self):
        text = """ 1 1
author Root
author-mail <root@example.com>
author-time 1700000000
author-tz +0000
summary root subject
filename src/a.c
\troot line
"""
        parsed = coverage_check.parse_git_blame_porcelain(text, {1})
        self.assertEqual(parsed[1]["commit"], "")
        self.assertTrue(parsed[1]["boundary"])

    def test_blame_porcelain_accepts_real_standalone_boundary_metadata(self):
        text = """abcdef0123456789abcdef0123456789abcdef01 1 1
author Root
author-mail <root@example.com>
author-time 1700000000
author-tz +0000
summary root subject
boundary
filename src/a.c
\troot line
"""
        parsed = coverage_check.parse_git_blame_porcelain(text, {1})
        self.assertEqual(parsed[1]["commit"], "abcdef0123456789abcdef0123456789abcdef01")
        self.assertTrue(parsed[1]["boundary"])

    @mock.patch("coverage_check._run_git_blame")
    def test_run_git_line_authors_coalesces_ranges_and_pins_newgit(self, mock_blame):
        mock_blame.return_value = """abc1234 10 10 2
author Alice
author-mail <alice@example.com>
author-time 1700000000
author-tz +0000
summary add lines
filename src/a.c
\tline ten
\tline eleven
abc1234 20 20
author Alice
author-mail <alice@example.com>
author-time 1700000000
author-tz +0000
summary add lines
filename src/a.c
\tline twenty
"""
        result = coverage_check.run_git_line_authors(
            self.temp_dir, "newgit-fixed", {"src/a.c": [10, 11, 20]}, "repo-a"
        )
        self.assertEqual(sorted(result["src/a.c"]), [10, 11, 20])
        self.assertEqual(result["src/a.c"][10]["repository"], "repo-a")
        calls = [call.args for call in mock_blame.call_args_list]
        self.assertEqual(calls[0][1], "newgit-fixed")
        self.assertEqual(calls[0][2], "src/a.c")
        self.assertEqual((calls[0][3], calls[0][4]), (10, 11))
        self.assertEqual((calls[1][3], calls[1][4]), (20, 20))

    @mock.patch("coverage_check._run_git_blame")
    def test_run_git_line_authors_whole_file_fallback_filters_fragments(self, mock_blame):
        selected = list(range(1, 52, 2))
        mock_blame.return_value = "\n".join(
            "abc1234 {} {}\nauthor Alice\nauthor-mail <alice@example.com>\n"
            "author-time 1700000000\nauthor-tz +0000\nsummary subject\n"
            "filename src/a.c\n\tline {}".format(line, line, line)
            for line in range(1, 53)
        )
        result = coverage_check.run_git_line_authors(
            self.temp_dir, "newgit", {"src/a.c": selected}, "repo"
        )
        self.assertEqual(sorted(result["src/a.c"]), selected)
        self.assertEqual(mock_blame.call_count, 1)
        self.assertEqual(len(mock_blame.call_args.args), 3)

    @mock.patch("coverage_check._run_git_blame", return_value="abc1234 1 1\n\ta\n")
    def test_blame_missing_requested_line_fails_closed(self, _mock_blame):
        with self.assertRaises(RuntimeError):
            coverage_check.run_git_line_authors(
                self.temp_dir, "newgit", {"src/a.c": [1, 2]}, "repo"
            )

    def test_schema_v3_attributes_and_precise_developer_tasks(self):
        diff = """--- a/src/a.c
+++ b/src/a.c
@@ -9,0 +10,3 @@
+covered();
+alice_missing();
+bob_missing();
"""
        authors = {
            "src/a.c": {
                10: {"author_name": "Alice", "author_email": "alice@example.com", "commit": "a1", "subject": "Alice"},
                11: {"author_name": "Alice", "author_email": "alice@example.com", "commit": "a1", "subject": "Alice"},
                12: {"author_name": "Bob", "author_email": "bob@example.com", "commit": "b1", "subject": "Bob"},
            }
        }
        result = coverage_check.calculate_repository_coverage(
            self.temp_dir, "old", "new", {"src/a.c": {10: 1, 11: 0, 12: 0}}, [],
            diff_text=diff,
            repository_name="repo-a",
            developer_file_changes=[
                {"repository": "repo-a", "file_path": "src/a.c", "change_type": "M",
                 "author_name": "Alice", "author_email": "alice@example.com", "commit": "a1"},
                {"repository": "repo-a", "file_path": "src/a.c", "change_type": "M",
                 "author_name": "Bob", "author_email": "bob@example.com", "commit": "b1"},
            ],
            line_authors_by_file=authors,
        )
        self.assertEqual(result["schema_version"], 3)
        self.assertEqual(result["details"][1]["author_email"], "alice@example.com")
        self.assertEqual(result["details"][2]["suggested_reviewer"], "Bob")
        self.assertEqual(result["reviewers_by_file"]["src/a.c"], {"11": "Alice", "12": "Bob"})
        developers = {item["email"]: item for item in result["developer_tasks"]["developers"]}
        self.assertEqual(developers["alice@example.com"]["files"][0]["owned_line_numbers"], [10, 11])
        self.assertEqual(developers["alice@example.com"]["files"][0]["uncovered_line_numbers"], [11])
        self.assertEqual(developers["bob@example.com"]["files"][0]["uncovered_line_numbers"], [12])

    def test_developer_summary_line_references_keep_file_identity(self):
        details = [
            {
                "repository": "repo-a", "file_path": "foo.c", "line_number": 10,
                "status": coverage_check.STATUS_UNCOVERED,
                "author_name": "Alice", "author_email": "alice@example.com",
            },
            {
                "repository": "repo-a", "file_path": "bar.c", "line_number": 10,
                "status": coverage_check.STATUS_UNCOVERED,
                "author_name": "Alice", "author_email": "alice@example.com",
            },
        ]
        developer = coverage_check.build_developer_tasks(details, {})["developers"][0]
        self.assertEqual(
            developer["owned_line_references"],
            ["repo-a:bar.c:10", "repo-a:foo.c:10"],
        )
        self.assertEqual(
            developer["uncovered_line_references"],
            ["repo-a:bar.c:10", "repo-a:foo.c:10"],
        )

    def test_shared_path_resolver_is_ambiguous_fail_closed(self):
        service = IncrementalService({
            "default": ["/repo-a/src/shared.c", "/repo-b/src/shared.c", "/repo-a/src/unique.c"]
        })
        value, match_type = service.resolve_mapping_value(
            "src/shared.c", {"/repo-a/src/shared.c": [1], "/repo-b/src/shared.c": [2]}
        )
        self.assertIsNone(value)
        self.assertEqual(match_type, "ambiguous_suffix")
        value, match_type = service.resolve_mapping_value(
            "src/unique.c", {"/repo-a/src/unique.c": [3]}
        )
        self.assertEqual(value, [3])
        self.assertEqual(match_type, "unique_suffix")
        value, match_type = service.resolve_mapping_value(
            "unique.c", {"/repo-a/src/unique.c": [3]}
        )
        self.assertIsNone(value)
        self.assertEqual(match_type, "basename_only_rejected")

    def test_shared_metadata_index_is_reused_for_repeated_resolves(self):
        mapping = {"/repo/src/a.c": [1], "/repo/src/b.c": [2]}
        service = IncrementalService({})
        self.assertEqual(service.resolve_mapping_value("src/a.c", mapping)[0], [1])
        self.assertEqual(service.resolve_mapping_value("src/b.c", mapping)[0], [2])
        self.assertEqual(len(service._mapping_indexes), 1)

    def test_multi_repository_same_relative_path_keeps_line_ownership_isolated(self):
        repo_a = os.path.join(self.temp_dir, "repo-a")
        repo_b = os.path.join(self.temp_dir, "repo-b")
        os.makedirs(repo_a)
        os.makedirs(repo_b)
        coverage_data = {
            os.path.join(repo_a, "src", "shared.c").replace(os.sep, "/"): {11: 0},
            os.path.join(repo_b, "src", "shared.c").replace(os.sep, "/"): {11: 0},
        }
        diff = "--- a/src/shared.c\n+++ b/src/shared.c\n@@ -10,0 +11 @@\n+pending();\n"
        repositories = [
            {"name": "repo-a", "path": repo_a, "oldgit": "old", "newgit": "new"},
            {"name": "repo-b", "path": repo_b, "oldgit": "old", "newgit": "new"},
        ]
        authors = {
            "repo-a": {"src/shared.c": {11: {"author_name": "Alice"}}},
            "repo-b": {"src/shared.c": {11: {"author_name": "Bob"}}},
        }
        with mock.patch("coverage_check.load_lcov_info_with_functions", return_value=(coverage_data, {}, ["fixture.info"])), \
                mock.patch("coverage_check.run_git_diff", return_value=diff), \
                mock.patch("coverage_check.run_git_developer_file_changes", return_value=[]):
            result = coverage_check.calculate_multi_repo_incremental_coverage(
                repositories, "fixture.info", line_authors_by_repo=authors
            )
        detail_by_repo = {item["repository"]: item for item in result["details"]}
        self.assertEqual(detail_by_repo["repo-a"]["suggested_reviewer"], "Alice")
        self.assertEqual(detail_by_repo["repo-b"]["suggested_reviewer"], "Bob")

    def test_lcov_complete_aliases_legacy_fallback_and_crossing_ranges(self):
        complete = self._write_info(
            "SF:src/a.c\nFN:2,5,foo\n"
            "FNL:0,6,8\nFNA:0,1,bar\n"
            "FNL:1,10,12\nFNA:1,0,baz\n"
            "DA:2,1\nend_of_record\n"
        )
        _, ranges = coverage_check.parse_lcov_info_data(complete)
        self.assertEqual(
            ranges["src/a.c"],
            [
                {"start_line": 2, "end_line": 5, "name": "foo"},
                {"start_line": 6, "end_line": 8, "name": "bar"},
                {"start_line": 10, "end_line": 12, "name": "baz"},
            ],
        )

        legacy = self._write_info("SF:src/a.c\nFN:2,foo\nDA:2,1\nend_of_record\n")
        _, legacy_ranges = coverage_check.parse_lcov_info_data(legacy)
        self.assertEqual(legacy_ranges, {})
        os.remove(legacy)

        incomplete_modern = self._write_info(
            "SF:src/a.c\nFNL:0,100\nFNA:0,5,foo\nDA:100,0\nend_of_record\n"
        )
        _, incomplete_ranges = coverage_check.parse_lcov_info_data(incomplete_modern)
        self.assertEqual(incomplete_ranges, {})
        os.remove(incomplete_modern)

        crossing = coverage_check.normalize_lcov_function_ranges([
            {"start_line": 1, "end_line": 5, "name": "a"},
            {"start_line": 4, "end_line": 8, "name": "b"},
        ])
        self.assertEqual(crossing, [])

    def test_multi_info_function_ranges_merge_and_conflict_fallback(self):
        first = os.path.join(self.temp_dir, "a.info")
        second = os.path.join(self.temp_dir, "b.info")
        with open(first, "w", encoding="utf-8") as stream:
            stream.write("SF:src/a.c\nFN:1,3,one\nDA:1,1\nend_of_record\n")
        with open(second, "w", encoding="utf-8") as stream:
            stream.write("SF:src/a.c\nFNL:0,5,7\nFNA:0,2,two\nDA:2,0\nend_of_record\n")
        _, ranges, _ = coverage_check.load_lcov_info_with_functions(self.temp_dir)
        self.assertEqual(len(ranges["src/a.c"]), 2)

        with open(second, "w", encoding="utf-8") as stream:
            stream.write("SF:src/a.c\nFNL:0,2,5\nFNA:0,2,two\nDA:2,0\nend_of_record\n")
        _, conflict_ranges, _ = coverage_check.load_lcov_info_with_functions(self.temp_dir)
        self.assertNotIn("src/a.c", conflict_ranges)

    def _html(self):
        return """<html><head><title>LCOV - cov - src/a.c</title></head><body><pre class="source">
<span class="lineNum"> 1 </span><span class="lineNoCov"> int a = 1;</span>
<span class="lineNum"> 2 </span><span class="lineNoCov"> int b = 2;</span>
<span class="lineNum"> 3 </span><span class="lineCov"> }</span>
</pre></body></html>"""

    def test_source_reader_splits_reviewer_blocks_and_preserves_db_precedence(self):
        ctx = parse_source_lines_from_gcov_html(
            self._html(), file_path="src/a.c", review_scope="incremental",
            incremental_line_numbers={1, 2},
            suggested_reviewers_by_line={1: "Alice", 2: "Bob"},
            analysis_records=[{"line_number": 1, "status": "未确认", "reviewer": "Carol", "is_draft": True}],
        )
        self.assertEqual(ctx.get_line(1).suggested_reviewer, "Alice")
        self.assertEqual(ctx.get_line(1).reviewer, "Carol")
        self.assertEqual(ctx.get_line(2).reviewer, "Bob")
        self.assertTrue(ctx.get_line(1).is_block_entry)
        self.assertTrue(ctx.get_line(2).is_block_entry)

    def test_source_reader_splits_on_effective_db_reviewer_and_overlay_refresh_preserves_suggestion(self):
        ctx = parse_source_lines_from_gcov_html(
            self._html(), file_path="src/a.c", review_scope="incremental",
            incremental_line_numbers={1, 2},
            suggested_reviewers_by_line={1: "Alice", 2: "Alice"},
            analysis_records=[
                {"line_number": 1, "status": "未确认", "reviewer": "Carol", "is_draft": True},
                {"line_number": 2, "status": "未确认", "reviewer": "Dave", "is_draft": True},
            ],
        )
        self.assertTrue(ctx.get_line(1).is_block_entry)
        self.assertTrue(ctx.get_line(2).is_block_entry)

        class FakeDB:
            def __init__(self):
                self.version = 1
                self.records = []

            def get_project_data_version(self, _project):
                return self.version

            def fetch_records(self, _project, _file):
                return list(self.records)

        db = FakeDB()
        service = CodeDetailService(db_manager=db)
        refreshed = parse_source_lines_from_gcov_html(
            self._html(), file_path="src/a.c", review_scope="incremental",
            incremental_line_numbers={1, 2},
            suggested_reviewers_by_line={1: "Alice", 2: "Alice"},
        )
        service._refresh_analysis_records(refreshed, "proj", "src/a.c", "incremental")
        self.assertEqual(refreshed.get_line(1).reviewer, "Alice")

        db.version = 2
        db.records = [{"line_number": 1, "status": "未确认", "reviewer": "Carol", "is_draft": True}]
        service._refresh_analysis_records(refreshed, "proj", "src/a.c", "incremental")
        self.assertEqual(refreshed.get_line(1).reviewer, "Carol")

    def test_parse_once_trusted_ranges_bypass_source_scanner_and_sidecar_round_trip(self):
        html = self._html().replace("int a = 1;", "int foo() {")
        with mock.patch("source_reader.extract_c_function_ranges", side_effect=AssertionError("fallback used")):
            artifact = parse_gcov_source_once(
                "proj", "report_1", "src/a.c", html,
                review_scope="incremental", incremental_lines={1, 2},
                suggested_reviewers_by_line={1: "Alice", 2: "Alice"},
                known_function_ranges=[{"start_line": 1, "end_line": 3, "name": "foo"}],
            )
        self.assertEqual(artifact.function_ranges[0].name, "foo")
        self.assertEqual(artifact.line_index_records[0]["function_name"], "foo")

        store = SidecarStore(search_dirs=[self.temp_dir], chunk_size=10)
        store.save_chunked_sidecar(self.temp_dir, "report_1", "file-key", artifact.source_context)
        loaded = store.load_full_source_context("report_1", "file-key")
        self.assertEqual(loaded.get_line(1).suggested_reviewer, "Alice")
        self.assertEqual(loaded.function_ranges[0].name, "foo")
        regions = build_code_regions(
            loaded.total_lines, [1, 2], loaded.function_ranges
        )
        self.assertTrue(any(
            region.default_state == "expanded" and
            region.start_line == 1 and region.end_line == 3
            for region in regions
        ))

    def test_invalid_known_ranges_fall_back_and_signature_hashes_change(self):
        with mock.patch("source_reader.extract_c_function_ranges", return_value=[FunctionRange(1, 3, "fallback")]) as scanner:
            ctx = parse_source_lines_from_gcov_html(
                self._html(), file_path="src/a.c", known_function_ranges=[{"start_line": 0, "end_line": 3}]
            )
        scanner.assert_called_once()
        self.assertEqual(ctx.function_ranges[0].name, "fallback")
        base = enhance_coverage.compute_directory_signature(
            self.temp_dir, project_name="p", review_scope="incremental",
            incremental_lines_by_file={"src/a.c": [1]},
            incremental_reviewers_by_file={"src/a.c": {"1": "Alice"}},
            function_ranges_by_file={"src/a.c": [{"start_line": 1, "end_line": 3, "name": "foo"}]},
        )
        changed_reviewer = enhance_coverage.compute_directory_signature(
            self.temp_dir, project_name="p", review_scope="incremental",
            incremental_lines_by_file={"src/a.c": [1]},
            incremental_reviewers_by_file={"src/a.c": {"1": "Bob"}},
            function_ranges_by_file={"src/a.c": [{"start_line": 1, "end_line": 3, "name": "foo"}]},
        )
        changed_range = enhance_coverage.compute_directory_signature(
            self.temp_dir, project_name="p", review_scope="incremental",
            incremental_lines_by_file={"src/a.c": [1]},
            incremental_reviewers_by_file={"src/a.c": {"1": "Alice"}},
            function_ranges_by_file={"src/a.c": [{"start_line": 1, "end_line": 4, "name": "foo"}]},
        )
        self.assertNotEqual(base["incremental_reviewer_set_hash"], changed_reviewer["incremental_reviewer_set_hash"])
        self.assertNotEqual(base["function_range_set_hash"], changed_range["function_range_set_hash"])

    def test_incremental_marker_escapes_reviewer_attribute(self):
        content = self._html()
        marked = enhance_coverage.mark_incremental_review_lines(
            content, {1}, {1: 'Alice "QA" & Bob'}
        )
        self.assertIn('data-coverage-reviewer="Alice &quot;QA&quot; &amp; Bob"', marked)


if __name__ == "__main__":
    unittest.main()
