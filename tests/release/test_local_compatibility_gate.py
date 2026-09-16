import json
import os
import tempfile
import unittest
from unittest import mock

from scripts.release.local_compatibility_gate import (
    MARIADB55_IMAGE, PYTHON36_IMAGE, run_gate,
)


class LocalCompatibilityGateTest(unittest.TestCase):
    def test_images_are_digest_pinned_and_not_floating(self):
        for image in (PYTHON36_IMAGE, MARIADB55_IMAGE):
            self.assertIn("@sha256:", image)
            self.assertNotIn(":latest", image)

    def test_missing_docker_is_not_verified_and_never_passes(self):
        with tempfile.TemporaryDirectory(prefix="compatibility-gate-") as root:
            output = os.path.join(root, "compatibility_tests.json")
            with mock.patch(
                    "scripts.release.local_compatibility_gate.shutil.which",
                    return_value=None):
                result = run_gate(os.getcwd(), output)
            self.assertEqual(result["status"], "NOT_VERIFIED")
            self.assertEqual(result["PYTHON36_COMPAT"], "NOT_VERIFIED")
            self.assertEqual(result["MARIADB55_COMPAT"], "NOT_VERIFIED")
            with open(output, encoding="utf-8") as stream:
                self.assertEqual(json.load(stream)["production_mutation"], "NONE")


if __name__ == "__main__":
    unittest.main()
