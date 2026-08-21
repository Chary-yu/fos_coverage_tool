import os
import sys
import tempfile
import unittest

from app.inheritance.toolchain import parser_toolchain_preflight


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


if __name__ == "__main__":
    unittest.main()
