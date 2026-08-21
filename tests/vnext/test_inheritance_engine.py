import json
import os
import sqlite3
import tempfile
import unittest

from app.inheritance.cpp_parser import CppSourceAnalyzer
from app.inheritance.engine import InheritanceEngine
from app.inheritance.line_map import GitLineMapEngine
from app.inheritance.normalizer import CppLexer, normalize_cpp
from scripts.diagnostics.inheritance_rules_audit import audit as audit_rules
from scripts.upgrade.migration_runner import create_sqlite_schema


class InheritanceEngineTest(unittest.TestCase):
    def test_lexer_preserves_literals_and_ignores_comments(self):
        source = r'''int f() { const char *x = "// not a comment"; /* comment */ return x[0]; }'''
        self.assertIn('"// not a comment"', normalize_cpp(source))
        self.assertNotIn("comment", normalize_cpp(source))
        self.assertEqual(normalize_cpp("a + b"), normalize_cpp("a/*x*/ +\tb"))
        self.assertNotEqual(normalize_cpp("a + b"), normalize_cpp("a - b"))

    def test_parser_identity_includes_scope_and_signature(self):
        analyzer = CppSourceAnalyzer()
        result = analyzer.analyze(
            "namespace n {\nclass C {\n int f(int x) const { return x; }\n};\n}\n",
            "src/a.cpp",
        )
        self.assertEqual(len(result["functions"]), 1)
        identity = result["functions"][0].identity
        self.assertEqual(identity.scope, ("n", "C"))
        self.assertEqual(identity.name, "f")
        self.assertIn("const", identity.qualifiers)

    def test_line_map_is_one_to_one_and_fail_closed_for_replacement(self):
        engine = GitLineMapEngine()
        mapped = engine.map_text(
            "int f() {\n  return 0;\n}\n",
            "int f() {\n  // comment\n  return 0;\n}\n",
        )
        self.assertEqual(mapped.get(2), 3)
        changed = engine.map_text("return a + b;\n", "return a - b;\n")
        self.assertIsNone(changed.get(1))
        self.assertIn(1, changed.ambiguous)

    def test_compare_line_allows_comment_only_change_and_rejects_control_change(self):
        engine = InheritanceEngine()
        old = "int f(int x) {\n if (x) {\n  return 1;\n }\n return 0;\n}\n"
        new = "int f(int x) {\n if (x) {\n  // comment\n  return 1;\n }\n return 0;\n}\n"
        old_analysis = CppSourceAnalyzer().analyze(old, "a.c")
        new_analysis = CppSourceAnalyzer().analyze(new, "a.c")
        result = engine.compare_line(
            "  return 1;", "  return 1;", old_analysis, new_analysis, 3, 4
        )
        self.assertTrue(result.ok)
        changed = CppSourceAnalyzer().analyze(
            "int f(int x) {\n if (x > 0) {\n  return 1;\n }\n}\n", "a.c"
        )
        rejected = engine.compare_line(
            "  return 1;", "  return 1;", old_analysis, changed, 3, 3
        )
        self.assertFalse(rejected.ok)
        self.assertEqual(rejected.reason_code, "CONTROL_CONTEXT_CHANGED")

    def test_rules_contract_contains_all_eighty_three_rules(self):
        result = audit_rules(os.getcwd())
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["rule_count"], 83)


if __name__ == "__main__":
    unittest.main()
