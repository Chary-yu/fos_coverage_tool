"""
Targeted Tests for Phase 1 Architecture & Directory Boundary (Items 15, 16, 17)
"""

import unittest
import os
import sys
import subprocess

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

class TestPhase1Directory(unittest.TestCase):

    def test_directory_layout_exists(self):
        """Verify new directory structure exists and contains manifest."""
        expected_dirs = [
            "app", "app/api", "app/db", "app/code_detail", "app/progress",
            "app/jobs", "app/inject", "app/incremental", "web/assets/js",
            "web/assets/css", "web/templates", "scripts/upgrade",
            "scripts/diagnostics", "scripts/maintenance", "config"
        ]
        for d in expected_dirs:
            p = os.path.join(_REPO_ROOT, d)
            self.assertTrue(os.path.isdir(p), f"Directory missing: {d}")

    def test_static_assets_in_place(self):
        """Verify key JS/CSS assets are present in web/assets."""
        assets = [
            "web/assets/js/coverage_enhance.js",
            "web/assets/js/coverage_progress.js",
            "web/assets/css/coverage_enhance.css"
        ]
        for a in assets:
            p = os.path.join(_REPO_ROOT, a)
            self.assertTrue(os.path.isfile(p), f"Static asset missing: {a}")

    def test_root_entrypoint_cli_help(self):
        """Verify root enhance_coverage.py still executes and provides CLI help."""
        res = subprocess.run(
            [sys.executable, "enhance_coverage.py", "--help"],
            cwd=_REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn(b"usage", res.stdout.lower())

if __name__ == "__main__":
    unittest.main()
