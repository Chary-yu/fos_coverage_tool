import os
import tempfile
import unittest

from app.release_identity import get_current_release_identity
from scripts.diagnostics.mysql_vnext_integration import (
    _prepare_runtime_release_identity, validate_runtime_version,
)


class MariaDBCompatibilityContractTests(unittest.TestCase):
    def test_disposable_integration_root_has_exact_release_manifest(self):
        with tempfile.TemporaryDirectory(prefix="mysql-integration-release-") as root:
            identity = _prepare_runtime_release_identity(root)
            observed = get_current_release_identity(root)
            self.assertEqual(observed["commit_sha"], identity["commit_sha"])
            self.assertEqual(observed["build_provenance"], "integration-fixture")
            self.assertTrue(os.path.isfile(os.path.join(root, "release_manifest.json")))

    def test_required_version_prefix_is_recorded(self):
        result = validate_runtime_version("5.5.64-MariaDB-1:5.5.64", "5.5")
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["required_version_prefix"], "5.5")

    def test_wrong_runtime_version_fails_closed(self):
        with self.assertRaises(ValueError):
            validate_runtime_version("11.8.8-MariaDB", "5.5")

    def test_empty_requirement_keeps_observed_version(self):
        result = validate_runtime_version("11.8.8-MariaDB")
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["required_version_prefix"], "")


if __name__ == "__main__":
    unittest.main()
