import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from app.inheritance.cpp_parser import CppSourceAnalyzer
from app.inheritance.engine import InheritanceEngine
from app.inheritance.dependencies import LazySourceAnalysisIndex, SourceAnalysisIndex
from app.inheritance.git_snapshot import GitSnapshotProvider
from app.inheritance.line_map import GitLineMapEngine
from app.inheritance.normalizer import CppLexer, normalize_cpp
from app.scan_import.publication import ScanPublicationService
from app.db.repositories import (
    AnalysisDomainRepository, LineIndexRepository, ProjectRepository,
    ProjectStateRepository, RepositoryRepository,
)
from app.services.project_service import ProjectService
from scripts.diagnostics.inheritance_rules_audit import audit as audit_rules
from scripts.upgrade.migration_runner import create_sqlite_schema


class InheritanceEngineTest(unittest.TestCase):
    @staticmethod
    def _git_fixture():
        root = tempfile.TemporaryDirectory(prefix="inheritance-git-")
        subprocess.check_call(["git", "init", "-q", root.name])
        subprocess.check_call(["git", "-C", root.name, "config", "user.email", "test@example.invalid"])
        subprocess.check_call(["git", "-C", root.name, "config", "user.name", "inheritance-test"])
        path = os.path.join(root.name, "src", "a.c")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("int f() {\n  return 0;\n}\n")
        subprocess.check_call(["git", "-C", root.name, "add", "."])
        subprocess.check_call(["git", "-C", root.name, "commit", "-qm", "old"])
        old_commit = subprocess.check_output(
            ["git", "-C", root.name, "rev-parse", "HEAD"], universal_newlines=True
        ).strip()
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("int f() {\n  // formatting-only context\n  return 0;\n}\n")
        subprocess.check_call(["git", "-C", root.name, "add", "."])
        subprocess.check_call(["git", "-C", root.name, "commit", "-qm", "new"])
        new_commit = subprocess.check_output(
            ["git", "-C", root.name, "rev-parse", "HEAD"], universal_newlines=True
        ).strip()
        return root, old_commit, new_commit

    def _inheritance_db_fixture(self, branch="main"):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        create_sqlite_schema(connection)
        root, old_commit, new_commit = self._git_fixture()
        projects = ProjectRepository()
        states = ProjectStateRepository()
        repositories = RepositoryRepository()
        service = ProjectService(
            projects, states, LineIndexRepository(), repository_repo=repositories
        )
        old = service.create_scan_and_ingest(
            connection, "engine-fixture", [{
                "repository_name": "repo-a", "file_path": "src/a.c",
                "file_path_hash": "o" * 32,
                "lines": [
                    {"line_number": 1, "line_text": "int f() {", "coverage_state": "covered"},
                    {"line_number": 2, "line_text": "  return 0;", "coverage_state": "uncovered"},
                    {"line_number": 3, "line_text": "}", "coverage_state": "covered"},
                ],
            }], repositories=[{
                "repository_name": "repo-a", "repository_path": root.name,
                "branch_name": "main", "commit_sha": old_commit,
                "old_commit_sha": old_commit, "new_commit_sha": old_commit,
                "identity_verified": True, "identity_provenance": "test",
            }], info_sha256="engine-old",
        )
        domain = AnalysisDomainRepository()
        old_file = projects.get_file(
            connection, old["id"], "repo-a", "o" * 32
        )
        old_line = connection.execute(
            "SELECT * FROM coverage_lines WHERE file_id=? AND line_number=2",
            (old_file["id"],),
        ).fetchone()
        record = domain.create_record(
            connection, {"status": "可覆盖", "coverage_method": "unit"},
            origin="MANUAL",
        )
        repo_id = connection.execute(
            "SELECT repository_id FROM coverage_scan_repositories WHERE scan_id=?",
            (old["id"],),
        ).fetchone()[0]
        block = domain.create_block(
            connection, old["id"], old_file["id"], 2, 2,
            record_id=record["id"], repository_id=repo_id, verified=True,
        )
        domain.create_link(
            connection, old["id"], old_line["id"], record["id"],
            block_id=block["id"], review_state="MANUAL_CONFIRMED",
            relation_origin="MANUAL",
        )
        candidate = service.create_scan_and_ingest(
            connection, "engine-fixture", [{
                "repository_name": "repo-a", "file_path": "src/a.c",
                "file_path_hash": "n" * 32,
                "lines": [
                    {"line_number": 1, "line_text": "int f() {", "coverage_state": "covered"},
                    {"line_number": 2, "line_text": "  // formatting-only context", "coverage_state": "covered"},
                    {"line_number": 3, "line_text": "  return 0;", "coverage_state": "uncovered"},
                    {"line_number": 4, "line_text": "}", "coverage_state": "covered"},
                ],
            }], repositories=[{
                "repository_name": "repo-a", "repository_path": root.name,
                "branch_name": branch, "commit_sha": new_commit,
                "old_commit_sha": old_commit, "new_commit_sha": new_commit,
                "identity_verified": True, "identity_provenance": "test",
            }], info_sha256="engine-new-" + branch,
        )
        return connection, root, old, candidate

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

    def test_git_hunk_recovery_does_not_cross_hunks(self):
        root, old_commit, new_commit = self._git_fixture()
        try:
            path = os.path.join(root.name, "src", "a.c")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(
                    "int f() {\n"
                    "  return 0;\n"
                    "  int a = 1;\n"
                    "  int b = 2;\n"
                    "  int c = 3;\n"
                    "  int d = 4;\n"
                    "  int e = 5;\n"
                    "  int f = 6;\n"
                    "  int g = 7;\n"
                    "}\n"
                )
            subprocess.check_call(["git", "-C", root.name, "add", "."])
            subprocess.check_call(["git", "-C", root.name, "commit", "-qm", "expanded"])
            expanded = subprocess.check_output(
                ["git", "-C", root.name, "rev-parse", "HEAD"], universal_newlines=True
            ).strip()
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(
                    "int f() {\n"
                    "  int a = 1;\n"
                    "  int b = 2;\n"
                    "  int c = 3;\n"
                    "  int d = 4;\n"
                    "  int e = 5;\n"
                    "  int f = 6;\n"
                    "  int g = 7;\n"
                    "  return 0;\n"
                    "}\n"
                )
            subprocess.check_call(["git", "-C", root.name, "add", "."])
            subprocess.check_call(["git", "-C", root.name, "commit", "-qm", "reintroduced"])
            reintroduced = subprocess.check_output(
                ["git", "-C", root.name, "rev-parse", "HEAD"], universal_newlines=True
            ).strip()
            mapping = GitLineMapEngine().map_git_file(
                root.name, expanded, reintroduced, "src/a.c"
            )
            self.assertIsNone(mapping.get(2))
            self.assertIn(2, mapping.deleted)
        finally:
            root.cleanup()

    def test_missing_history_fetches_from_configured_remote(self):
        provider = GitSnapshotProvider("/tmp/repository", fetch_remote="origin")
        with mock.patch.object(
                provider, "commit_available", side_effect=[False, True]
        ) as available, mock.patch.object(provider, "_run", return_value="") as run:
            self.assertTrue(provider.ensure_commit("deadbeef"))
        self.assertEqual(available.call_count, 2)
        run.assert_called_once_with(
            ["fetch", "--no-tags", "origin", "deadbeef"]
        )

    def test_engine_uses_candidate_file_id_for_each_physical_line(self):
        connection, root, old, candidate = self._inheritance_db_fixture()
        try:
            result = InheritanceEngine().run(
                connection, candidate["id"], repository_paths={"repo-a": root.name},
                batch_size=1,
            )
            candidate_line = connection.execute(
                "SELECT l.id FROM coverage_lines l JOIN coverage_files f ON f.id=l.file_id "
                "WHERE f.scan_id=? AND f.file_path=? AND l.line_number=3",
                (candidate["id"], "src/a.c"),
            ).fetchone()[0]
            decision = connection.execute(
                "SELECT candidate_line_id, decision, reason_code "
                "FROM coverage_inheritance_decisions WHERE candidate_scan_id=? "
                "AND candidate_line_id=?",
                (candidate["id"], candidate_line),
            ).fetchone()
            self.assertEqual(result["inherited"], 1)
            self.assertEqual(tuple(decision), (candidate_line, "INHERITED", "INHERITED"))
            self.assertEqual(result["read_set"]["format"], "inheritance-read-set-v1")
            self.assertEqual(result["read_set"]["relations"]["count"], 1)
            self.assertEqual(result["read_set"]["records"]["count"], 1)
            self.assertEqual(result["metrics"]["source_relation_page_peak"], 1)
            self.assertEqual(result["metrics"]["target_line_page_peak"], 1)
            ScanPublicationService._validate_compact_read_set(
                connection, result["read_set"], candidate["id"], old["id"]
            )
            source_record_id = connection.execute(
                "SELECT analysis_record_id FROM coverage_analysis_line_links "
                "WHERE scan_id=? AND is_active=1", (old["id"],)
            ).fetchone()[0]
            connection.execute(
                "UPDATE coverage_analysis_records SET content_revision="
                "content_revision + 1 WHERE id=?", (source_record_id,)
            )
            with self.assertRaises(ValueError) as raised:
                ScanPublicationService._validate_compact_read_set(
                    connection, result["read_set"], candidate["id"], old["id"]
                )
            self.assertEqual(str(raised.exception), "READ_SET_CHANGED")
        finally:
            root.cleanup()
            connection.close()

    def test_engine_hard_blocks_repository_branch_mismatch(self):
        connection, root, old, candidate = self._inheritance_db_fixture(branch="feature")
        try:
            result = InheritanceEngine().run(
                connection, candidate["id"], repository_paths={"repo-a": root.name}
            )
            reasons = [row[0] for row in connection.execute(
                "SELECT reason_code FROM coverage_inheritance_decisions "
                "WHERE candidate_scan_id=?", (candidate["id"],)
            ).fetchall()]
            self.assertEqual(result["inherited"], 0)
            self.assertIn("BRANCH_MISMATCH", reasons)
        finally:
            root.cleanup()
            connection.close()

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

    def test_dependency_budget_exhaustion_is_not_treated_as_external_library(self):
        """A source index that cannot load a helper must fail closed."""
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
            "int helper() {\n return 1;\n}\n", "src/helper.cpp"
        )

        def load_old(path):
            return old_helper, 1024

        def load_new(path):
            return new_helper, 1024

        old_index = LazySourceAnalysisIndex(
            paths=["src/helper.cpp"], loader=load_old,
            analyses={"src/caller.cpp": old_caller}, max_cached_bytes=1,
        )
        new_index = LazySourceAnalysisIndex(
            paths=["src/helper.cpp"], loader=load_new,
            analyses={"src/caller.cpp": new_caller}, max_cached_bytes=1,
        )
        result = InheritanceEngine().compare_line(
            " return helper();", " return helper();",
            old_caller, new_caller, 2, 2,
            old_index=old_index, new_index=new_index,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "DEPENDENCY_BUDGET_EXHAUSTED")

    def test_lazy_dependency_resolution_uses_candidate_paths_and_negative_cache(self):
        """Ordinary/absent tokens must not trigger a repository-wide parser scan."""
        analyzer = CppSourceAnalyzer()
        helper = analyzer.analyze(
            "int helper() { return 1; }\n", "src/helper.cpp"
        )
        loaded = []

        def load(path):
            loaded.append(path)
            return helper, 1024

        index = LazySourceAnalysisIndex(
            paths=["src/a.cpp", "src/b.cpp", "src/helper.cpp"],
            loader=load,
            candidate_index={
                "functions": {"helper": ["src/helper.cpp"]},
                "macros": {}, "constants": {},
            },
            max_cached_bytes=4096,
        )
        self.assertEqual(index.functions("return"), [])
        self.assertEqual(index.functions("return"), [])
        self.assertEqual(index.functions("missing_symbol"), [])
        self.assertEqual(index.functions("missing_symbol"), [])
        self.assertEqual(loaded, [])
        self.assertEqual(len(index.functions("helper")), 1)
        self.assertEqual(loaded, ["src/helper.cpp"])
        self.assertGreaterEqual(index.cache_stats()["resolution_cache_entries"], 2)

    def test_candidate_index_is_sound_over_parser_function_corpus(self):
        """Lexical candidates must recall every function accepted by parser."""
        sources = {
            "src/trailing.cpp": (
                "namespace demo {\n"
                "class Box {\n"
                "  auto member(int value) const noexcept -> int {\n"
                "    return value;\n"
                "  }\n"
                "};\n"
                "}\n"
            ),
            "src/long_signature.cpp": (
                "int long_signature({}) {{\n"
                "  return 1;\n"
                "}}\n"
            ).format(", ".join(
                "int parameter_{}".format(number) for number in range(90)
            )),
            "src/qualified.cpp": (
                "int qualified(int left, int right) noexcept {\n"
                "  return left + right;\n"
                "}\n"
            ),
        }
        provider = GitSnapshotProvider("/candidate-index-repo")
        with mock.patch.object(
                provider, "read_files",
                side_effect=lambda commit, paths: (
                    (path, sources[path]) for path in paths
                )):
            candidates = provider.build_symbol_candidate_index(
                "commit", sorted(sources)
            )

        analyzer = CppSourceAnalyzer()
        for path, source in sources.items():
            analysis = analyzer.analyze(source, path)
            for function in analysis["functions"]:
                self.assertIn(
                    path, candidates["functions"].get(function.identity.name, ()),
                    "{} was not recalled for {}".format(
                        function.identity.name, path
                    ),
                )

    def test_trailing_return_candidate_cannot_hide_changed_callee(self):
        """A trailing-return helper remains a same-repository dependency."""
        analyzer = CppSourceAnalyzer()
        caller = analyzer.analyze(
            "int caller() {\n return helper();\n}\n", "src/caller.cpp"
        )
        old_helper = analyzer.analyze(
            "auto helper() -> int {\n return 1;\n}\n", "src/helper.cpp"
        )
        new_helper = analyzer.analyze(
            "auto helper() -> int {\n return 2;\n}\n", "src/helper.cpp"
        )
        provider = GitSnapshotProvider("/candidate-index-repo")
        with mock.patch.object(
                provider, "read_files",
                return_value=[(
                    "src/helper.cpp",
                    "auto helper() -> int {\n return 1;\n}\n",
                )]):
            candidates = provider.build_symbol_candidate_index(
                "commit", ["src/helper.cpp"]
            )

        old_index = LazySourceAnalysisIndex(
            paths=["src/helper.cpp"],
            loader=lambda path: (old_helper, 1024),
            analyses={"src/caller.cpp": caller},
            candidate_index=candidates,
        )
        new_index = LazySourceAnalysisIndex(
            paths=["src/helper.cpp"],
            loader=lambda path: (new_helper, 1024),
            analyses={"src/caller.cpp": caller},
            candidate_index=candidates,
        )
        result = InheritanceEngine().compare_line(
            " return helper();", " return helper();",
            caller, caller, 2, 2,
            old_index=old_index, new_index=new_index,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "CALLEE_CHANGED")

    def test_constant_candidate_index_is_sound_over_parser_corpus(self):
        """Const candidates recall logical-line and long-type definitions."""
        long_type = "::".join(["ns"] * 90 + ["Type"])
        sources = {
            "src/constants.hpp": (
                "constexpr int " + "\\" + "\n"
                "FEATURE_FLAG = 1;\n"
                "const int ORDINARY_FLAG = 2;\n"
                "const {} QUALIFIED_FLAG = 3;\n"
                "#define MACRO_FLAG 1\n"
            ).format(long_type),
        }
        provider = GitSnapshotProvider("/candidate-index-repo")
        with mock.patch.object(
                provider, "read_files",
                side_effect=lambda commit, paths: (
                    (path, sources[path]) for path in paths
                )):
            candidates = provider.build_symbol_candidate_index(
                "commit", sorted(sources)
            )

        analysis = CppSourceAnalyzer().analyze(
            sources["src/constants.hpp"], "src/constants.hpp"
        )
        self.assertEqual(
            set(analysis["constants"]),
            {"FEATURE_FLAG", "ORDINARY_FLAG", "QUALIFIED_FLAG"},
        )
        for name in analysis["constants"]:
            self.assertIn(
                "src/constants.hpp",
                candidates["constants"].get(name, ()),
                "{} was not recalled by constant candidate index".format(name),
            )
        self.assertIn(
            "src/constants.hpp",
            candidates["macros"].get("MACRO_FLAG", ()),
        )

    def test_line_spliced_constant_candidate_cannot_hide_changed_dependency(self):
        """A changed constexpr on a logical line must block inheritance."""
        analyzer = CppSourceAnalyzer()
        caller = analyzer.analyze(
            "int caller() {\n return FEATURE_FLAG;\n}\n",
            "src/caller.cpp",
        )
        old_source = "constexpr int " + "\\" + "\nFEATURE_FLAG = 1;\n"
        new_source = "constexpr int " + "\\" + "\nFEATURE_FLAG = 2;\n"
        old_constant = analyzer.analyze(old_source, "src/constants.hpp")
        new_constant = analyzer.analyze(new_source, "src/constants.hpp")

        def candidate_for(source):
            provider = GitSnapshotProvider("/candidate-index-repo")
            with mock.patch.object(
                    provider, "read_files",
                    return_value=[("src/constants.hpp", source)]):
                return provider.build_symbol_candidate_index(
                    "commit", ["src/constants.hpp"]
                )

        old_index = LazySourceAnalysisIndex(
            paths=["src/constants.hpp"],
            loader=lambda path: (old_constant, 1024),
            analyses={"src/caller.cpp": caller},
            candidate_index=candidate_for(old_source),
        )
        new_index = LazySourceAnalysisIndex(
            paths=["src/constants.hpp"],
            loader=lambda path: (new_constant, 1024),
            analyses={"src/caller.cpp": caller},
            candidate_index=candidate_for(new_source),
        )
        result = InheritanceEngine().compare_line(
            " return FEATURE_FLAG;", " return FEATURE_FLAG;",
            caller, caller, 2, 2,
            old_index=old_index, new_index=new_index,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "CONST_CHANGED")

    def test_candidate_index_uses_one_git_batch_process_per_commit(self):
        root = tempfile.TemporaryDirectory(prefix="candidate-batch-git-")
        try:
            paths = ["src/a.cpp", "src/b.hpp", "src/c.c"]
            for index, path in enumerate(paths):
                absolute = os.path.join(root.name, path)
                os.makedirs(os.path.dirname(absolute), exist_ok=True)
                with open(absolute, "w", encoding="utf-8") as stream:
                    stream.write(
                        "int helper_{}() {{ return {}; }}\n".format(index, index)
                    )
            subprocess.check_call(["git", "init", "-q", root.name])
            subprocess.check_call([
                "git", "-C", root.name, "config", "user.email",
                "test@example.invalid",
            ])
            subprocess.check_call([
                "git", "-C", root.name, "config", "user.name",
                "candidate-batch-test",
            ])
            subprocess.check_call(["git", "-C", root.name, "add", "."])
            subprocess.check_call([
                "git", "-C", root.name, "commit", "-qm", "candidate batch",
            ])
            commit = subprocess.check_output(
                ["git", "-C", root.name, "rev-parse", "HEAD"],
                universal_newlines=True,
            ).strip()
            provider = GitSnapshotProvider(root.name)
            with mock.patch(
                    "app.inheritance.git_snapshot.subprocess.Popen",
                    wraps=subprocess.Popen) as popen:
                candidates = provider.build_symbol_candidate_index(
                    commit, paths
                )
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(
                sorted(candidates["functions"]),
                ["helper_0", "helper_1", "helper_2"],
            )
        finally:
            root.cleanup()

    def test_engine_wires_lightweight_candidate_index_before_parser_loading(self):
        class Provider(object):
            repo_path = "/candidate-index-repo"

            def __init__(self):
                self.reads = []

            def list_source_files(self, commit):
                return ["src/file-{}.cpp".format(number) for number in range(1000)]

            def build_symbol_candidate_index(self, commit, paths):
                return {
                    "functions": {"helper": ["src/helper.cpp"]},
                    "macros": {}, "constants": {},
                }

            def read_file(self, commit, path):
                self.reads.append(path)
                return "int helper() { return 1; }\n"

        provider = Provider()
        engine = InheritanceEngine()
        engine._metrics = {
            "parser_cache_hit": 0, "parser_cache_miss": 0,
            "source_files_total": 0, "parser_file_total": 0,
        }
        index = engine._source_index(provider, "commit")
        self.assertEqual(index.functions("ordinary_token"), [])
        self.assertEqual(index.functions("ordinary_token"), [])
        self.assertEqual(provider.reads, [])
        self.assertEqual(len(index.functions("helper")), 1)
        self.assertEqual(provider.reads, ["src/helper.cpp"])

    def test_single_side_dependency_budget_exhaustion_is_unknown(self):
        analyzer = CppSourceAnalyzer()
        caller = analyzer.analyze(
            "int caller() {\n return helper();\n}\n", "src/caller.cpp"
        )
        helper = analyzer.analyze(
            "int helper() { return 1; }\n", "src/helper.cpp"
        )
        exhausted = LazySourceAnalysisIndex(
            paths=["src/helper.cpp"], loader=lambda path: (helper, 4096),
            analyses={"src/caller.cpp": caller}, max_cached_bytes=1,
        )
        available = SourceAnalysisIndex(
            {"src/caller.cpp": caller, "src/helper.cpp": helper}
        )
        result = InheritanceEngine().compare_line(
            " return helper();", " return helper();",
            caller, caller, 2, 2,
            old_index=exhausted, new_index=available,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "DEPENDENCY_BUDGET_EXHAUSTED")

    def test_pinned_initial_index_over_budget_is_explicitly_exhausted(self):
        analyzer = CppSourceAnalyzer()
        caller = analyzer.analyze("int caller() { return 0; }\n", "src/caller.cpp")
        index = LazySourceAnalysisIndex(
            analyses={"src/caller.cpp": caller}, max_cached_bytes=1,
        )
        result = InheritanceEngine().compare_line(
            "return 0;", "return 0;", caller, caller, 1, 1,
            old_index=index, new_index=None,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "DEPENDENCY_BUDGET_EXHAUSTED")

    def test_engine_source_index_cache_has_a_total_budget_across_commits(self):
        analyzer = CppSourceAnalyzer()

        class Provider(object):
            repo_path = "/repo"

            def list_source_files(self, commit):
                return ["src/helper.cpp"]

            def read_file(self, commit, path):
                return "int helper() { return 1; }\n"

        provider = Provider()
        engine = InheritanceEngine()
        engine.max_source_cache_bytes = 1024 * 1024
        engine._metrics = {
            "parser_cache_hit": 0, "parser_cache_miss": 0,
            "source_files_total": 0, "parser_file_total": 0,
        }
        first = engine._source_index(provider, "commit-one")
        first.functions("helper")
        engine.max_source_cache_total_bytes = first.cache_stats()["cache_bytes"] + 1
        second = engine._source_index(provider, "commit-two")
        second.functions("helper")
        self.assertLessEqual(len(engine._source_index_cache), 1)
        self.assertEqual(engine._metrics["source_index_evictions"], 1)

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
