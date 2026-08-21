import os
import sys
import tempfile
import unittest

from scripts.diagnostics.deterministic_inheritance_corpus import (
    DEFAULT_FIXTURE, derived_reports, run,
)


class DeterministicInheritanceCorpusTest(unittest.TestCase):
    def test_local_corpus_has_no_known_false_positive(self):
        result = run(DEFAULT_FIXTURE)
        self.assertEqual(result["status"], "PASSED", result)
        self.assertTrue(result["synthetic"])
        self.assertFalse(result["release_eligible"])
        self.assertEqual(result["failed_cases"], 0)
        reports = derived_reports(result)
        self.assertEqual(
            reports["false_positive_check"]["known_false_positive_count"], 0
        )
        self.assertEqual(reports["parser_uncertainty_report"]["status"], "PASSED")
        self.assertEqual(reports["dependency_resolution_report"]["status"], "PASSED")

    def test_fixture_is_inside_repository_and_repeated_result_is_identical(self):
        self.assertTrue(os.path.isfile(DEFAULT_FIXTURE))
        self.assertEqual(run(DEFAULT_FIXTURE), run(DEFAULT_FIXTURE))

    def test_external_parser_runs_the_same_corpus_after_toolchain_preflight(self):
        # This helper deliberately delegates parsing to the repository parser
        # only to make the subprocess fixture self-contained.  The corpus is
        # still executed through ExternalJsonCppParserAdapter, so a missing or
        # malformed external protocol cannot silently fall back to builtin.
        helper_source = r'''
import json
import sys

sys.path.insert(0, %r)
from app.inheritance.cpp_parser import CppSourceAnalyzer

if "--version" in sys.argv:
    print("fixture-parser 1")
    raise SystemExit(0)
if "--analyze-json" not in sys.argv:
    raise SystemExit(2)

request = json.load(sys.stdin)
analysis = CppSourceAnalyzer().analyze(request["source"], request["path"])

def encode_map(value):
    return {str(key): list(item) for key, item in (value or {}).items()}

functions = []
for item in analysis.get("functions", []):
    identity = item.identity
    functions.append({
        "identity": {
            "path": identity.path,
            "scope": list(identity.scope),
            "name": identity.name,
            "parameters": list(identity.parameters),
            "qualifiers": list(identity.qualifiers),
            "trailing_return": list(identity.trailing_return),
        },
        "start_line": item.start_line,
        "end_line": item.end_line,
        "body_tokens": list(item.body_tokens),
        "uncertain": item.uncertain,
    })
payload = {
    "protocol": "coverage-cpp-parser-v1",
    "analysis": {
        "supported": analysis.get("supported", True),
        "functions": functions,
        "controls": encode_map(analysis.get("controls")),
        "preprocessor": encode_map(analysis.get("preprocessor")),
        "macros": encode_map(analysis.get("macros")),
        "constants": encode_map(analysis.get("constants")),
        "calls": encode_map(analysis.get("calls")),
        "lines": analysis.get("lines", []),
        "tokens": analysis.get("tokens", []),
        "path": analysis.get("path", request["path"]),
        "uncertain": analysis.get("uncertain", False),
    },
}
print(json.dumps(payload))
''' % os.path.abspath(os.getcwd())
        with tempfile.TemporaryDirectory(prefix="deterministic-parser-") as directory:
            helper = os.path.join(directory, "helper.py")
            with open(helper, "w") as stream:
                stream.write(helper_source)
            result = run(
                DEFAULT_FIXTURE,
                command=[sys.executable, helper],
                adapter_name="json-cli-v1",
                require_external=True,
            )
            repeated = run(
                DEFAULT_FIXTURE,
                command=[sys.executable, helper],
                adapter_name="json-cli-v1",
                require_external=True,
            )
        self.assertEqual(result["status"], "PASSED", result)
        self.assertFalse(result["synthetic"])
        self.assertTrue(result["parser_external"])
        self.assertEqual(result["parser_backend"], "json-cli-v1")
        self.assertEqual(result["parser_toolchain"]["status"], "PASSED")
        self.assertEqual(result["failed_cases"], 0)
        self.assertEqual(result, repeated)

    def test_external_parser_preflight_failure_is_not_replaced_by_builtin(self):
        result = run(
            DEFAULT_FIXTURE,
            command="parser-command-that-does-not-exist",
            adapter_name="json-cli-v1",
            require_external=True,
        )
        self.assertEqual(result["status"], "FAILED")
        self.assertFalse(result["parser_external"])
        self.assertEqual(result["parser_toolchain"]["status"], "FAILED")
        self.assertEqual(result["reason_counts"], {"PARSER_TOOLCHAIN_FAILURE": 1})


if __name__ == "__main__":
    unittest.main()
