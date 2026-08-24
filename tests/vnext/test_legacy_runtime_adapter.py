import subprocess
import sys
import unittest
import importlib
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

    def test_legacy_module_assignment_updates_previous_release_globals(self):
        legacy = importlib.import_module("enhance_coverage")
        previous = importlib.import_module(
            "app.compat.legacy_runtime_previous_release"
        )
        old_manager = previous.db_manager
        fake_manager = object()
        try:
            legacy.db_manager = fake_manager
            handler = legacy.CoverageHTTPRequestHandler
            self.assertIs(previous.db_manager, fake_manager)
            self.assertIs(handler.do_POST.__globals__["db_manager"], fake_manager)
        finally:
            previous.db_manager = old_manager
            legacy.__dict__.pop("db_manager", None)


if __name__ == "__main__":
    unittest.main()
