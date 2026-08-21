import json
import os
import sqlite3
import tempfile
import unittest

from app.inheritance.cpp_parser import CppSourceAnalyzer
from app.inheritance.engine import InheritanceEngine
from app.inheritance.dependencies import SourceAnalysisIndex
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

    def test_lexer_handles_encoded_raw_strings_and_line_splicing(self):
        raw = 'const char *x = u8R"tag(// /* not comments */)tag";'
        self.assertIn('u8R"tag(// /* not comments */)tag"', normalize_cpp(raw))
        spliced = "if (ready && " + chr(92) + "\nvalue) return 1;"
        self.assertEqual(
            normalize_cpp(spliced),
            normalize_cpp("if (ready && value) return 1;"),
        )

    def test_parser_preserves_spliced_control_and_preprocessor_context(self):
        analyzer = CppSourceAnalyzer()
        splice = chr(92) + "\n"
        source = (
            "#if ENABLED " + splice
            + " && FEATURE\n"
            + "int f(int x) { if (x && " + splice
            + "x > 0) { return 1; } return 0; }\n"
            + "#endif\n"
        )
        result = analyzer.analyze(source, "a.cpp")
        self.assertIn("if ENABLED && FEATURE", " ".join(result["preprocessor"].get(3, ())))
        self.assertEqual(result["controls"].get(3), result["controls"].get(4))

    def test_parser_discards_preprocessor_tokens_before_function_headers(self):
        analyzer = CppSourceAnalyzer()
        old = analyzer.analyze(
            "#if ENABLED\nint f() { return 1; }\n#endif\n", "a.cpp"
        )
        new = analyzer.analyze(
            "#if FEATURE\nint f() { return 1; }\n#endif\n", "a.cpp"
        )
        self.assertEqual([item.identity.name for item in old["functions"]], ["f"])
        result = InheritanceEngine().compare_line(
            "int f() { return 1; }", "int f() { return 1; }",
            old, new, 2, 2,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "PP_CONTEXT_CHANGED")

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

    def test_control_context_covers_same_physical_line_header_and_body(self):
        analyzer = CppSourceAnalyzer()
        old = analyzer.analyze("int f(int x) { if (x) { return 1; } return 0; }", "a.c")
        new = analyzer.analyze("int f(int x) { if (x > 0) { return 1; } return 0; }", "a.c")
        self.assertNotEqual(old["controls"].get(1), new["controls"].get(1))
        result = InheritanceEngine().compare_line(
            "return 1;", "return 1;",
            old, new, 1, 1,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "CONTROL_CONTEXT_CHANGED")

    def test_stateful_lexer_never_promotes_multiline_comment_to_a_call(self):
        analysis = CppSourceAnalyzer().analyze(
            "int f() {\n /* helper();\n    fake(); */\n return 1;\n}\n", "a.c"
        )
        self.assertFalse(analysis["uncertain"])
        self.assertEqual(analysis["calls"].get(3), ())
        uncertain = CppSourceAnalyzer().analyze(
            "int f() {\n /* unterminated\n return 1;\n}\n", "a.c"
        )
        self.assertTrue(uncertain["uncertain"])

    def test_dependency_index_checks_same_repository_helper_in_another_file(self):
        analyzer = CppSourceAnalyzer()
        old_caller = analyzer.analyze(
            "int caller() {\n return helper();\n}\n", "src/caller.cpp"
        )
        new_caller = analyzer.analyze(
            "int caller() {\n return helper();\n}\n", "src/caller.cpp"
        )
        old_helper = analyzer.analyze(
            "int helper() {\n return 1;\n}\n", "src/helper.cpp"
        )
        new_helper = analyzer.analyze(
            "int helper() {\n return 2;\n}\n", "src/helper.cpp"
        )
        old_index = SourceAnalysisIndex({
            "src/caller.cpp": old_caller, "src/helper.cpp": old_helper,
        })
        new_index = SourceAnalysisIndex({
            "src/caller.cpp": new_caller, "src/helper.cpp": new_helper,
        })
        result = InheritanceEngine().compare_line(
            " return helper();", " return helper();",
            old_caller, new_caller, 2, 2,
            old_index=old_index, new_index=new_index,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "CALLEE_CHANGED")

    def test_durable_run_requires_verified_git_snapshot_identity(self):
        result = InheritanceEngine()._snapshot_for_relation(
            {"file_path": "src/a.c", "source_line_text": "return 0;"},
            "", {}, {},
        )
        self.assertEqual(result["reason_code"], "REPOSITORY_IDENTITY_UNVERIFIED")

    def test_read_set_is_deterministic_and_deduplicated(self):
        relations = [
            {
                "id": 9,
                "relation_revision": 4,
                "analysis_record_id": 21,
                "source_content_revision": 7,
            },
            {
                "id": 3,
                "relation_revision": 2,
                "analysis_record_id": 8,
                "source_content_revision": 5,
            },
            {
                "id": 9,
                "relation_revision": 4,
                "analysis_record_id": 21,
                "source_content_revision": 7,
            },
        ]
        self.assertEqual(
            InheritanceEngine._read_set_for_relations(relations),
            [
                {"relation_id": 3, "relation_revision": 2},
                {"relation_id": 9, "relation_revision": 4},
                {"record_id": 8, "content_revision": 5},
                {"record_id": 21, "content_revision": 7},
            ],
        )

    def test_rules_contract_contains_all_eighty_three_rules(self):
        result = audit_rules(os.getcwd())
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["rule_count"], 83)
        self.assertEqual(result["test_id_count"], 83)
        self.assertEqual(result["mapped_test_id_count"], 83)
        self.assertEqual(result["missing_owner_modules"], [])
        self.assertEqual(result["invalid_test_selectors"], [])
        self.assertTrue(result["plan_sha256_match"])


if __name__ == "__main__":
    unittest.main()
