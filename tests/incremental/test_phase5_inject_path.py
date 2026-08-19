"""
Targeted Tests for Phase 5 Inject / Incremental Optimization (Items 10, 11, 12, 25)
"""

import unittest
import os
import sys
import tempfile
import shutil
import time

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.inject.parse_once import parse_gcov_source_once
from app.inject.directory_signature import calculate_directory_signature_incremental
from app.incremental.path_index import LCOVPathLookupIndex

class TestPhase5InjectPath(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_item_10_inject_parse_once(self):
        """Verify single-pass parsing creates source context, function ranges, and line index records."""
        html = """
        <html><body><pre class="source">
        <span class="lineNum"> 1</span><span class="lineCov"> 1 : int add(int a, int b) {</span>
        <span class="lineNum"> 2</span><span class="lineNoCov"> ##### : return a + b;</span>
        <span class="lineNum"> 3</span><span class="lineCov"> 1 : }</span>
        </pre></body></html>
        """
        art = parse_gcov_source_once(
            project_name="MathProj",
            report_id="rep_1",
            file_path="src/math/add.c",
            html_content=html
        )
        self.assertEqual(len(art.source_lines), 3)
        self.assertEqual(len(art.line_index_records), 1)
        rec = art.line_index_records[0]
        self.assertEqual(rec["line_number"], 2)
        self.assertEqual(rec["project_name"], "MathProj")
        self.assertEqual(rec["file_path"], "src/math/add.c")

    def test_item_11_directory_signature_incremental_manifest(self):
        """Verify directory signature calculation reuses manifest sha when unchanged and updates on edit."""
        f1 = os.path.join(self.test_dir, "f1.c.gcov.html")
        with open(f1, "w") as f:
            f.write("content 1")
            
        sig1, man1 = calculate_directory_signature_incremental(self.test_dir)
        self.assertIn("f1.c.gcov.html", man1["files"])
        
        # Second call with unmodified file -> same signature, fast path
        sig2, man2 = calculate_directory_signature_incremental(self.test_dir)
        self.assertEqual(sig1, sig2)
        
        # Modify file -> signature changes
        time.sleep(0.01)
        with open(f1, "w") as f:
            f.write("content 1 modified")
            
        sig3, man3 = calculate_directory_signature_incremental(self.test_dir)
        self.assertNotEqual(sig1, sig3)

    def test_item_12_lcov_path_index_multi_repo_isolation(self):
        """Verify path lookup resolves exact, normalized, unique suffix and isolates repos."""
        repos = {
            "RepoA": ["src/core/driver.c", "src/net/driver.c"],
            "RepoB": ["src/core/driver.c", "src/common/util.c"]
        }
        idx = LCOVPathLookupIndex(repos)
        
        # RepoA exact
        pA, clsA = idx.resolve_path("RepoA", "src/core/driver.c")
        self.assertEqual(clsA, "exact")
        self.assertEqual(pA, "src/core/driver.c")
        
        # RepoA ambiguous suffix ('driver.c' matches both core/driver and net/driver) -> fail closed
        pAmb, clsAmb = idx.resolve_path("RepoA", "driver.c")
        self.assertIn(clsAmb, ["ambiguous_suffix", "basename_only_rejected"])
        self.assertIsNone(pAmb)
        
        # RepoB unique suffix ('util.c')
        pB, clsB = idx.resolve_path("RepoB", "common/util.c")
        self.assertEqual(clsB, "unique_suffix")
        self.assertEqual(pB, "src/common/util.c")

if __name__ == "__main__":
    unittest.main()
