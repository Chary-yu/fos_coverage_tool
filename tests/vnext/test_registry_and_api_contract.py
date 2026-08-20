import os
import tempfile
import unittest

from app.reports.registry import ReportRegistry


class ReportRegistryTest(unittest.TestCase):
    def test_exact_report_lookup_and_prune_are_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="report-registry-") as root:
            output = os.path.join(root, "report")
            os.makedirs(output)
            registry = ReportRegistry(os.path.join(root, "registry"))
            registry.register("report_a", [output], sidecar_required=True)
            self.assertEqual(registry.load_exact("report_a")["directories"], [output])
            with self.assertRaises(ValueError):
                registry.load_exact("../report_a")
            self.assertEqual(registry.resolve_exact_root("report_a"), None)
            self.assertEqual(registry.prune(), ["report_a"])
            self.assertIsNone(registry.load_exact("report_a"))


if __name__ == "__main__":
    unittest.main()
