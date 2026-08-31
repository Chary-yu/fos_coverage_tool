import subprocess
import sys
import unittest
import importlib
import os
import tempfile
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

    def test_incremental_module_proxy_reads_and_assigns_old_symbols(self):
        legacy = importlib.import_module("app.incremental.legacy")
        previous = importlib.import_module(
            "app.compat.incremental_previous_release"
        )
        old_blame = previous._run_git_blame
        fake_blame = lambda *args, **kwargs: ""
        try:
            self.assertIs(legacy._run_git_blame, old_blame)
            legacy._run_git_blame = fake_blame
            self.assertIs(previous._run_git_blame, fake_blame)
        finally:
            legacy._run_git_blame = old_blame

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

    def test_legacy_module_class_proxy_reads_old_only_symbols(self):
        legacy = importlib.import_module("app.legacy_runtime")
        previous = importlib.import_module(
            "app.compat.legacy_runtime_previous_release"
        )
        self.assertIs(
            legacy.CoverageHTTPRequestHandler,
            previous.CoverageHTTPRequestHandler,
        )

    def test_legacy_progress_page_does_not_load_vnext_cursor_endpoints(self):
        previous = importlib.import_module(
            "app.compat.legacy_runtime_previous_release"
        )
        with tempfile.TemporaryDirectory(prefix="legacy-progress-page-") as root:
            output_dir = os.path.join(root, "output")
            os.makedirs(output_dir)
            previous.write_progress_page_targets(output_dir, output_dir)
            with open(
                os.path.join(output_dir, "coverage_progress.html"),
                encoding="utf-8",
            ) as stream:
                contents = stream.read()
        self.assertIn("/progress?project=", contents)
        self.assertNotIn("/progress/files", contents)
        self.assertNotIn("/progress/details", contents)
        self.assertNotIn('src="coverage_progress.js', contents)


if __name__ == "__main__":
    unittest.main()
