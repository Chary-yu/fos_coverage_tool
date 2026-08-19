#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Unit tests for coverage_check.py module."""

import os
import tempfile
import unittest
from unittest import mock

import coverage_check


class TestCoverageCheck(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_normalize_path_and_source_extension_filter(self):
        self.assertEqual(coverage_check.normalize_path("src\\main.c"), "src/main.c")
        self.assertEqual(coverage_check.normalize_path("./src/sub/../main.c"), "src/main.c")

        self.assertTrue(coverage_check.is_valid_source_file("src/main.c"))
        self.assertTrue(coverage_check.is_valid_source_file("include/header.H"))
        self.assertTrue(coverage_check.is_valid_source_file("src/main.cpp"))
        self.assertTrue(coverage_check.is_valid_source_file("src/engine.cc"))
        self.assertTrue(coverage_check.is_valid_source_file("include/math.hpp"))
        self.assertFalse(coverage_check.is_valid_source_file("README.md"))
        self.assertFalse(coverage_check.is_valid_source_file("script.py"))
        self.assertFalse(coverage_check.is_valid_source_file(""))

    def test_parse_diff_text_unified_diff(self):
        sample_diff = """diff --git a/src/main.c b/src/main.c
new file mode 100644
index 0000000..1234567
--- /dev/null
+++ b/src/main.c
@@ -0,0 +1,4 @@
+#include <stdio.h>
+int main() {
+    printf("hello");
+    return 0;
+}
diff --git a/src/test.txt b/src/test.txt
--- a/src/test.txt
+++ b/src/test.txt
@@ -1,1 +1,2 @@
+ignored txt line
"""
        result = coverage_check.parse_diff_text(sample_diff)
        self.assertIn("src/main.c", result)
        self.assertEqual(result["src/main.c"], [1, 2, 3, 4, 5])
        self.assertNotIn("src/test.txt", result)

    def test_parse_lcov_info(self):
        lcov_content = """SF:src/main.c
DA:1,1
DA:2,0
DA:3,1
end_of_record
SF:src/helper.h
DA:10,0
end_of_record
"""
        info_file = os.path.join(self.temp_dir.name, "coverage.info")
        with open(info_file, "w", encoding="utf-8") as f:
            f.write(lcov_content)

        result = coverage_check.parse_lcov_info(info_file)
        self.assertIn("src/main.c", result)
        self.assertEqual(result["src/main.c"][1], 1)
        self.assertEqual(result["src/main.c"][2], 0)
        self.assertIn("src/helper.h", result)
        self.assertEqual(result["src/helper.h"][10], 0)

    def test_run_git_developer_file_changes_parsing(self):
        git_log_output = (
            "1234567890abcdef\x1fAlice\x1falice@example.com\x1f2026-08-13T12:00:00+08:00\x1fAdd main feature\n"
            "M\tsrc/main.c\n"
            "M\tREADME.md\n"
        )
        mock_proc = mock.MagicMock()
        mock_proc.communicate.return_value = (git_log_output.encode("utf-8"), b"")
        mock_proc.returncode = 0

        with mock.patch("subprocess.Popen", return_value=mock_proc):
            changes = coverage_check.run_git_developer_file_changes("/fake/repo", "v1", "v2", "repo_a")

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["author_name"], "Alice")
        self.assertEqual(changes[0]["file_path"], "src/main.c")
        self.assertEqual(changes[0]["repository"], "repo_a")

    def test_run_git_diff_failure_raises_runtime_error(self):
        mock_proc = mock.MagicMock()
        mock_proc.communicate.return_value = (b"", b"fatal: bad revision")
        mock_proc.returncode = 128

        with mock.patch("subprocess.Popen", return_value=mock_proc):
            with self.assertRaises(RuntimeError) as ctx:
                coverage_check.run_git_diff("/fake/repo", "v1", "v2")
            self.assertIn("git diff v1 v2 failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
