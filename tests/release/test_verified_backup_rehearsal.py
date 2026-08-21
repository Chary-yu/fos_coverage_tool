import gzip
import hashlib
import os
import tempfile
import unittest

from scripts.upgrade.run_verified_backup_rehearsal import (
    _load_expected_sha,
    _safe_database_name,
    _validate_dump,
)


class TestVerifiedBackupRehearsal(unittest.TestCase):
    def test_disposable_database_names_are_namespaced(self):
        self.assertEqual(
            _safe_database_name("coverage_gate_a_source_abc", "source"),
            "coverage_gate_a_source_abc",
        )
        with self.assertRaises(ValueError):
            _safe_database_name("coverage", "source")
        with self.assertRaises(ValueError):
            _safe_database_name("coverage_gate_a_source;DROP", "source")

    def test_dump_requires_hash_and_external_storage(self):
        with tempfile.TemporaryDirectory() as root:
            deploy = os.path.join(root, "candidate")
            os.makedirs(deploy)
            dump = os.path.join(root, "verified", "full.sql.gz")
            os.makedirs(os.path.dirname(dump))
            with gzip.open(dump, "wb") as stream:
                stream.write(b"CREATE TABLE coverage_example (id INT);\n")
            with open(dump, "rb") as stream:
                digest = hashlib.sha256(stream.read()).hexdigest()
            resolved, actual, expanded = _validate_dump(
                dump, digest, deploy, [deploy],
            )
            self.assertEqual(resolved, os.path.realpath(dump))
            self.assertEqual(actual, digest)
            self.assertGreater(expanded, 0)

            with self.assertRaises(ValueError):
                _validate_dump(dump, "0" * 64, deploy, [deploy])

    def test_dump_sidecar_hash_is_supported(self):
        with tempfile.TemporaryDirectory() as root:
            dump = os.path.join(root, "full.sql.gz")
            with gzip.open(dump, "wb") as stream:
                stream.write(b"fixture")
            with open(dump, "rb") as stream:
                digest = hashlib.sha256(stream.read()).hexdigest()
            with open(dump + ".sha256", "w") as stream:
                stream.write("{}  full.sql.gz\n".format(digest))
            self.assertEqual(_load_expected_sha(dump, ""), digest)


if __name__ == "__main__":
    unittest.main()
