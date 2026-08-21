import json
import os
import tempfile
import unittest
from unittest import mock

from app.compat.telemetry import record


class LegacyTelemetryTest(unittest.TestCase):
    def test_record_preserves_deprecation_metadata_and_increments_atomically(self):
        with tempfile.TemporaryDirectory(prefix="legacy-telemetry-") as root:
            path = os.path.join(root, "usage.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({
                    "window_started_at": "2026-08-01T00:00:00Z",
                    "window_ends_at": "2026-09-01T00:00:00Z",
                }, stream)
            with mock.patch.dict(
                    os.environ, {"COVERAGE_LEGACY_USAGE_FILE": path},
                    clear=False):
                record("enhance_coverage:cli")
                record("enhance_coverage:cli")
            with open(path, "r", encoding="utf-8") as stream:
                payload = json.load(stream)
            self.assertEqual(payload["enhance_coverage:cli"], 2)
            self.assertEqual(payload["window_started_at"], "2026-08-01T00:00:00Z")
            self.assertTrue(os.path.isfile(path + ".lock"))


if __name__ == "__main__":
    unittest.main()
