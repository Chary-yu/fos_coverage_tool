"""
Targeted Tests for Phase 6 Chunked Sidecar & Legacy Compatibility (Items 13, 14, 22)
"""

import unittest
import os
import sys
import tempfile
import shutil
import json

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from source_reader import SourceContext, SourceLineDTO, save_source_sidecar, calc_sidecar_file_key
from app.code_detail.sidecar_store import SidecarStore
from scripts.diagnostics.sidecar_registry_audit import audit_sidecar_and_registry

class TestPhase6Sidecar(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.store = SidecarStore(search_dirs=[self.test_dir], chunk_size=50)
        self.registry_dir = tempfile.mkdtemp()
        self.previous_registry_dir = os.environ.get("COVERAGE_REGISTRY_DIR")
        os.environ["COVERAGE_REGISTRY_DIR"] = self.registry_dir

    def tearDown(self):
        if self.previous_registry_dir is None:
            os.environ.pop("COVERAGE_REGISTRY_DIR", None)
        else:
            os.environ["COVERAGE_REGISTRY_DIR"] = self.previous_registry_dir
        shutil.rmtree(self.test_dir, ignore_errors=True)
        shutil.rmtree(self.registry_dir, ignore_errors=True)

    def test_item_14_legacy_sidecar_reading(self):
        """Verify SidecarStore correctly reads legacy v1 .source.json format."""
        report_id = "rep_legacy"
        file_path = "src/legacy/foo.c"
        file_key = calc_sidecar_file_key(file_path)
        
        lines = [SourceLineDTO(line_no=i, source=f"code_{i}", coverage_state="covered") for i in range(1, 101)]
        ctx = SourceContext(
            project_name="LegacyProj",
            file_path=file_path,
            lines=lines,
            function_ranges=[],
            report_id=report_id
        )
        save_source_sidecar(self.test_dir, report_id, file_key, ctx)
        
        # 1. Read metadata
        meta = self.store.load_metadata(report_id, file_key)
        self.assertIsNotNone(meta)
        self.assertEqual(meta["total_lines"], 100)
        self.assertEqual(meta["schema_version"], 1)
        
        # 2. Read range (lines 10..20)
        range_lines = self.store.load_lines_range(report_id, file_key, 10, 20)
        self.assertEqual(len(range_lines), 11)
        self.assertEqual(range_lines[0]["line_no"], 10)
        self.assertEqual(range_lines[-1]["line_no"], 20)

    def test_item_13_chunked_v2_sidecar_creation_and_range_read(self):
        """Verify SidecarStore writes Chunked v2 format and performs partial chunk reading."""
        report_id = "rep_chunked"
        file_path = "src/chunked/bar.c"
        file_key = calc_sidecar_file_key(file_path)
        
        lines = [SourceLineDTO(line_no=i, source=f"line_{i}", coverage_state="covered") for i in range(1, 151)]
        ctx = SourceContext(
            project_name="ChunkProj",
            file_path=file_path,
            lines=lines,
            function_ranges=[],
            report_id=report_id
        )
        
        # Save chunked (chunk_size=50 -> 3 chunks: 0..49, 50..99, 100..149)
        cache_dir = self.store.save_chunked_sidecar(self.test_dir, report_id, file_key, ctx)
        self.assertTrue(os.path.isfile(os.path.join(cache_dir, "meta.json")))
        self.assertTrue(os.path.isfile(os.path.join(cache_dir, "lines-000000-000049.json")))
        self.assertTrue(os.path.isfile(os.path.join(cache_dir, "lines-000050-000099.json")))
        self.assertTrue(os.path.isfile(os.path.join(cache_dir, "lines-000100-000149.json")))
        
        # Read metadata
        meta = self.store.load_metadata(report_id, file_key)
        self.assertEqual(meta["schema_version"], 2)
        self.assertEqual(meta["total_chunks"], 3)
        
        # Read range spanning chunk 1 and chunk 2 (lines 40..60)
        slice_lines = self.store.load_lines_range(report_id, file_key, 40, 60)
        self.assertEqual(len(slice_lines), 21)
        self.assertEqual(slice_lines[0]["line_no"], 40)
        self.assertEqual(slice_lines[-1]["line_no"], 60)

    def test_item_22_sidecar_audit(self):
        """Verify sidecar audit recognizes both legacy and chunked formats."""
        for report_id in ("rep_legacy", "rep_chunked"):
            with open(os.path.join(self.registry_dir, report_id + ".json"), "w", encoding="utf-8") as stream:
                json.dump({
                    "report_id": report_id,
                    "directories": [self.test_dir],
                    "sidecar_required": False,
                }, stream)
        res = audit_sidecar_and_registry([self.test_dir])
        self.assertTrue(res["is_safe"])

if __name__ == "__main__":
    unittest.main()
