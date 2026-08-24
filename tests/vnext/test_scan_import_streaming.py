import os
import hashlib
import tempfile
import unittest
from unittest import mock

from app.inject.service import ScanImportService


class ScanImportStreamingTest(unittest.TestCase):
    def test_import_does_not_materialize_all_lcov_records_before_ingest(self):
        class CapturingProjectService(object):
            def create_scan_and_ingest(self, connection, project_name, files, **kwargs):
                self.consumed = []
                for item in files:
                    self.consumed.append(item["file_path"])
                    list(item["lines"])
                return {"id": 1}

        project_service = CapturingProjectService()
        service = ScanImportService(project_service)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".info", delete=False) as stream:
            stream.write(
                "TN:\nSF:src/first.c\nDA:1,0\nend_of_record\n"
                "TN:\nSF:src/second.c\nDA:2,1\nend_of_record\n"
            )
            info_path = stream.name
        try:
            with mock.patch(
                    "app.inject.service.parse_info",
                    side_effect=AssertionError("full LCOV materialization")):
                result = service.import_info(None, "streaming", info_path)
        finally:
            os.remove(info_path)
        self.assertEqual(project_service.consumed, ["src/first.c", "src/second.c"])
        self.assertEqual(result["files"], 2)
        self.assertEqual(result["line_count"], 2)

    def test_recovery_verification_returns_observed_sha_without_materializing(self):
        content = "TN:\nSF:src/recovery.c\nDA:1,0\nend_of_record\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".info", delete=False) as stream:
            stream.write(content)
            info_path = stream.name
        try:
            expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
            service = ScanImportService()
            observed, records, stats = service.iter_info_file(
                info_path, expected_sha256=expected, verify=True,
            )
            self.assertEqual(observed, expected)
            self.assertEqual([item["file_path"] for item in records], ["src/recovery.c"])
            self.assertEqual(stats["files"], 1)
        finally:
            os.remove(info_path)
