import subprocess
import sys
import unittest
from unittest import mock

from app.compat import legacy_runtime_adapter


class LegacyRuntimeAdapterTest(unittest.TestCase):
    def test_import_does_not_eagerly_load_large_legacy_implementation(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import app.legacy_runtime; "
                "print('app.compat.legacy_runtime_impl' in sys.modules)",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")

    def test_incremental_import_does_not_eagerly_load_legacy_implementation(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import app.incremental.legacy; "
                "print('app.compat.incremental_impl' in sys.modules)",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")

    def test_incremental_adapter_does_not_eagerly_load_previous_release_code(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import app.compat.incremental_impl; "
                "print('app.compat.incremental_previous_release' in sys.modules)",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")

    def test_vnext_server_surface_delegates_to_bootstrap(self):
        config = {"runtime_mode": "vnext", "server": {"host": "127.0.0.1", "port": 1}}
        with mock.patch.object(legacy_runtime_adapter, "load_config", return_value=config), \
                mock.patch.object(legacy_runtime_adapter, "_run_vnext_server") as start:
            legacy_runtime_adapter.run_server("candidate.json")
        start.assert_called_once_with(config)


if __name__ == "__main__":
    unittest.main()
