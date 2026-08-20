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

    def test_registry_does_not_merge_duplicate_roots_or_store_sidecar_directory(self):
        with tempfile.TemporaryDirectory(prefix="report-registry-roots-") as root:
            first = os.path.join(root, "first")
            second = os.path.join(root, "second")
            os.makedirs(os.path.join(first, ".source_cache", "report_a"))
            os.makedirs(second)
            registry = ReportRegistry(os.path.join(root, "registry"))
            registry.register(
                "report_a", [os.path.join(first, ".source_cache", "report_a")]
            )
            self.assertEqual(registry.resolve_exact_root("report_a"), first)
            with self.assertRaises(ValueError):
                registry.register("report_a", [second])
            registry.register("report_a", [second], replace=True)
            self.assertEqual(registry.load_exact("report_a")["directories"], [second])


if __name__ == "__main__":
    unittest.main()
