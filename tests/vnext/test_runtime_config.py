import json
import os
import tempfile
import unittest

from app.config.runtime_config import load_application_config
from scripts.config_preflight import preflight_config
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

    def test_candidate_lifecycle_commands_and_rollback_are_distinct(self):
        config = load_application_config(
            os.path.join(os.getcwd(), "config/coverage_config.staging.example.json"),
            base_dir=os.getcwd(),
        )
        upgrade = config["upgrade"]
        self.assertNotEqual(
            upgrade["release_endpoint"], upgrade["previous_release_endpoint"]
        )
        self.assertNotEqual(
            upgrade["commands"]["start_validation_api"],
            upgrade["commands"]["start_previous_api"],
        )
        self.assertNotEqual(
            upgrade["commands"]["stop_current_api"],
            upgrade["commands"]["stop_validation_api"],
        )
        self.assertNotEqual(
            upgrade["commands"]["start_validation_api"],
            upgrade["commands"]["start_serving_api"],
        )
        self.assertNotEqual(
            upgrade["commands"]["stop_validation_api"],
            upgrade["commands"]["stop_serving_api"],
        )
        self.assertTrue(upgrade["candidate_root"])
        self.assertTrue(upgrade["publish_root"])
        self.assertNotEqual(upgrade["candidate_root"], upgrade["publish_root"])
        self.assertTrue(upgrade["validation_session_manifest"])
        self.assertTrue(upgrade["validation_teardown_evidence_path"])
        self.assertEqual(upgrade["serving_session_id"], "current-serving")
        self.assertTrue(upgrade["serving_session_manifest"])
        self.assertTrue(upgrade["current_serving_state_path"])
        self.assertEqual(upgrade["validation_ports"], [19528])

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

    def test_legacy_config_preflight_writes_independent_candidate_and_preserves_source(self):
        with tempfile.TemporaryDirectory(prefix="config-preflight-") as root:
            source = os.path.join(root, "v10.json")
            candidate = os.path.join(root, "candidate", "vnext.json")
            original = {
                "database": {"host": "legacy-db", "port": 3307},
                "server": {"bind": "127.0.0.1", "port": 9529},
                "runtime_state": {"root": "legacy-state"},
            }
            with open(source, "w", encoding="utf-8") as stream:
                json.dump(original, stream, sort_keys=True)
            with open(source, "rb") as stream:
                before = stream.read()
            result = preflight_config(source, base_dir=root, output_path=candidate,
                                      write_candidate=True)
            self.assertEqual(result["status"], "REVIEW_REQUIRED")
            self.assertTrue(result["diff"])
            self.assertTrue(result["source_unchanged"])
            with open(source, "rb") as stream:
                self.assertEqual(stream.read(), before)
            with open(candidate, "r", encoding="utf-8") as stream:
                upgraded = json.load(stream)
            self.assertEqual(upgraded["config_schema_version"], 2)
            self.assertEqual(upgraded["mysql"]["host"], "legacy-db")
            self.assertEqual(upgraded["server"]["host"], "127.0.0.1")

    def test_production_old_config_schema_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="config-production-") as root:
            path = os.path.join(root, "old.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({"runtime_mode": "vnext", "schema_version": 1}, stream)
            old_env = os.environ.get("COVERAGE_ENV")
            os.environ["COVERAGE_ENV"] = "production"
            try:
                with self.assertRaisesRegex(RuntimeError, "config_schema_version"):
                    load_application_config(path, base_dir=root)
            finally:
                if old_env is None:
                    os.environ.pop("COVERAGE_ENV", None)
                else:
                    os.environ["COVERAGE_ENV"] = old_env


if __name__ == "__main__":
    unittest.main()
