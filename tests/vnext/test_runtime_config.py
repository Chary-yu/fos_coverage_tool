import json
import os
import tempfile
import unittest

from app.config.runtime_config import load_application_config
from app.legacy_runtime import get_arg_value, load_config


class RuntimeConfigTest(unittest.TestCase):
    def test_server_config_argument_selects_candidate_without_legacy_fallback(self):
        with tempfile.TemporaryDirectory(prefix="vnext-server-config-") as root:
            path = os.path.join(root, "candidate.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({
                    "runtime_mode": "vnext",
                    "schema_version": 1,
                    "mysql": {"database": "coverage_candidate"},
                    "server": {"port": 19528},
                }, stream)

            config_arg = get_arg_value(["--config", path], "--config")
            config = load_config(config_arg)

            self.assertEqual(config["runtime_mode"], "vnext")
            self.assertEqual(config["mysql"]["database"], "coverage_candidate")
            self.assertEqual(config["server"]["port"], 19528)
            with self.assertRaises(FileNotFoundError):
                load_config(os.path.join(root, "missing.json"))

    def test_default_application_config_selects_canonical_vnext_runtime(self):
        config = load_application_config(None, base_dir=os.getcwd())
        self.assertEqual(config["runtime_mode"], "vnext")
        self.assertEqual(config["schema_version"], 1)
        self.assertEqual(config["server"]["host"], "127.0.0.1")

    def test_candidate_rollback_endpoint_and_start_command_are_distinct(self):
        config = load_application_config(
            os.path.join(os.getcwd(), "config/coverage_config.staging.example.json"),
            base_dir=os.getcwd(),
        )
        upgrade = config["upgrade"]
        self.assertNotEqual(
            upgrade["release_endpoint"], upgrade["previous_release_endpoint"]
        )
        self.assertNotEqual(
            upgrade["commands"]["start_api"],
            upgrade["commands"]["start_previous_api"],
        )

    def test_candidate_roots_are_resolved_relative_to_their_declared_base(self):
        with tempfile.TemporaryDirectory(prefix="vnext-config-") as root:
            path = os.path.join(root, "coverage.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({
                    "runtime_mode": "vnext",
                    "schema_version": 1,
                    "runtime_state": {
                        "root": "state",
                        "exports_dir": "exports",
                    },
                    "input_roots": ["inputs"],
                    "report_roots": ["reports"],
                }, stream)

            config = load_application_config(path, base_dir=root)

            self.assertEqual(
                config["runtime_state"]["exports_dir"],
                os.path.realpath(os.path.join(root, "state", "exports")),
            )
            self.assertEqual(
                config["input_roots"],
                [os.path.realpath(os.path.join(root, "inputs"))],
            )
            self.assertEqual(
                config["report_roots"],
                [os.path.realpath(os.path.join(root, "reports"))],
            )


if __name__ == "__main__":
    unittest.main()
