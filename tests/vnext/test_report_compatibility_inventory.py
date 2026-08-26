import os
import tempfile
import unittest

from scripts.diagnostics.report_compatibility_inventory import inventory


class ReportCompatibilityInventoryTest(unittest.TestCase):
    def test_inventory_is_read_only_and_classifies_versioned_reports(self):
        with tempfile.TemporaryDirectory(prefix="report-inventory-") as root:
            os.makedirs(os.path.join(root, "assets"))
            with open(os.path.join(root, "assets", "progress.js"), "w") as stream:
                stream.write("asset-v1")
            with open(os.path.join(root, "current.html"), "w") as stream:
                stream.write(
                    '<meta name="api_contract_version" content="vnext-api-20260826.1">'
                    '<script src="assets/progress.js"></script>'
                    '<script>var x={"report_id":"report-a",'
                    '"scan_id":7,"commit_sha":"%s"};'
                    'fetch("/api/coverage/progress")</script>' % ("a" * 40)
                )
            with open(os.path.join(root, "legacy.html"), "w") as stream:
                stream.write('<script src="/legacy.js"></script>')
            result = inventory([root], repo_root=root)
            self.assertTrue(result["read_only"])
            self.assertEqual(result["files_scanned"], 2)
            self.assertEqual(result["classification_counts"]["CANONICAL_VNEXT"], 1)
            self.assertEqual(result["files_missing_identity"], 1)
            current = next(item for item in result["reports"]
                           if item["path"] == "current.html")
            self.assertEqual(current["metadata"]["report_id"], ["report-a"])
            self.assertEqual(current["asset_references"][0]["sha256"],
                             __import__("hashlib").sha256(b"asset-v1").hexdigest())


if __name__ == "__main__":
    unittest.main()
