import os
import sqlite3
import sys
import tempfile
import unittest

from app.bootstrap import VNextRuntime
from app.inheritance.toolchain import (
    ExternalJsonCppParserAdapter, parser_from_config,
    parser_toolchain_preflight, parser_toolchain_preflight_from_config,
)
from scripts.upgrade.migration_runner import create_sqlite_schema


class ParserToolchainTest(unittest.TestCase):
    def test_missing_external_parser_fails_closed(self):
        result = parser_toolchain_preflight(
            command="coverage-parser-command-that-does-not-exist",
            adapter_name="clang-cli",
            require_external=True,
        )
        self.assertEqual(result["status"], "FAILED")
        self.assertFalse(result["production_ready"])

    def test_builtin_parser_is_not_release_ready(self):
        result = parser_toolchain_preflight(
            command=sys.executable,
            adapter_name="builtin-conservative",
        )
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertFalse(result["production_ready"])
        self.assertTrue(result["binary_sha256"])

    def test_unregistered_adapter_cannot_claim_production_readiness(self):
        result = parser_toolchain_preflight(
            command=sys.executable,
            adapter_name="verified-test-adapter",
        )
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertFalse(result["production_ready"])
        self.assertTrue(any("not registered" in item for item in result["violations"]))

    def test_registered_json_adapter_passes_version_and_protocol_smoke(self):
        helper_source = r'''
import json
import sys

if "--version" in sys.argv:
    print("fixture-parser 1")
    raise SystemExit(0)
if "--analyze-json" not in sys.argv:
    raise SystemExit(2)
request = json.load(sys.stdin)
print(json.dumps({
    "protocol": "coverage-cpp-parser-v1",
    "analysis": {
        "supported": True,
        "path": request["path"],
        "functions": [{
            "path": request["path"], "scope": [], "name": "preflight_function",
            "parameters": [], "qualifiers": [], "trailing_return": [],
            "start_line": 1, "end_line": 1, "body_tokens": ["return", "0"]
        }],
        "controls": {}, "preprocessor": {}, "macros": {},
        "constants": {}, "calls": {}, "uncertain": False
    }
}))
'''
        with tempfile.TemporaryDirectory(prefix="parser-adapter-") as directory:
            helper = os.path.join(directory, "helper.py")
            with open(helper, "w") as stream:
                stream.write(helper_source)
            command = [sys.executable, helper]
            result = parser_toolchain_preflight(
                command=command, adapter_name="json-cli-v1",
                require_external=True,
            )
            self.assertEqual(result["status"], "PASSED")
            self.assertTrue(result["production_ready"])
            self.assertEqual(result["smoke_test"], "PASSED")
            configured = parser_toolchain_preflight_from_config({
                "inheritance_parser": {
                    "adapter": "json-cli-v1", "command": command,
                    "require_external": True,
                },
            })
            self.assertEqual(configured["status"], "PASSED")
            self.assertTrue(configured["configured"])
            self.assertEqual(
                configured["configuration_source"], "inheritance_parser"
            )
            parser = parser_from_config({
                "inheritance_parser": {
                    "adapter": "json-cli-v1", "command": command,
                    "require_external": True,
                }
            })
            self.assertIsInstance(parser, ExternalJsonCppParserAdapter)
            analysis = parser.analyze(
                "int preflight_function() { return 0; }\n", "src/a.c"
            )
            self.assertEqual(parser.function_for_line(analysis, 1).identity.name,
                             "preflight_function")
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            create_sqlite_schema(connection)
            runtime = VNextRuntime({
                "project_name": "parser-fixture",
                "auth": {"mode": "disabled"},
                "runtime_state": {"root": os.path.join(directory, "state")},
                "inheritance_parser": {
                    "adapter": "json-cli-v1", "command": command,
                    "require_external": True,
                },
            }, os.getcwd(), connection=connection)
            try:
                self.assertIsInstance(
                    runtime.scan_import_coordinator.inheritance.parser,
                    ExternalJsonCppParserAdapter,
                )
            finally:
                runtime.close()
                connection.close()

    def test_external_adapter_protocol_mismatch_fails_closed(self):
        helper_source = r'''
import sys
if "--version" in sys.argv:
    print("fixture-parser 1")
elif "--analyze-json" in sys.argv:
    print("{\"protocol\": \"wrong-protocol\"}")
else:
    raise SystemExit(2)
'''
        with tempfile.TemporaryDirectory(prefix="parser-adapter-bad-") as directory:
            helper = os.path.join(directory, "helper.py")
            with open(helper, "w") as stream:
                stream.write(helper_source)
            result = parser_toolchain_preflight(
                command=[sys.executable, helper], adapter_name="json-cli-v1",
                require_external=True,
            )
            self.assertEqual(result["status"], "FAILED")
            self.assertFalse(result["production_ready"])
            self.assertIn("protocol version mismatch", result["violations"][0])


if __name__ == "__main__":
    unittest.main()
