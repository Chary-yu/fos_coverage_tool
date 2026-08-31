import json
import os
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

from app.compat import legacy_runtime_previous_release as legacy


class LegacyBackgroundSerializationTest(unittest.TestCase):
    def test_nested_decimal_background_result_is_atomic_and_readable(self):
        job_id = "decimal-background-test"
        job = {"id": job_id, "kind": "progress", "state": "running"}
        with tempfile.TemporaryDirectory(prefix="legacy-background-") as root:
            with mock.patch.dict(legacy._background_jobs, {job_id: job}, clear=False), \
                    mock.patch.object(legacy, "get_background_jobs_storage_dir",
                                      return_value=root), \
                    mock.patch.object(legacy, "save_job_to_db"):
                legacy._finish_background_job(
                    job_id,
                    state="completed",
                    data={
                        "top": Decimal("1.50"),
                        "nested": {"whole": Decimal("2"),
                                   "items": (Decimal("3.25"),)},
                    },
                )
                result_path = legacy._background_jobs[job_id]["result_path"]
                self.assertTrue(os.path.isfile(result_path))
                with open(result_path, encoding="utf-8") as stream:
                    result = json.load(stream)
            self.assertEqual(result["top"], 1.5)
            self.assertEqual(result["nested"]["whole"], 2)
            self.assertEqual(result["nested"]["items"], [3.25])


if __name__ == "__main__":
    unittest.main()
