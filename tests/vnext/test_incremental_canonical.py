import os
import sqlite3
import subprocess
import tempfile
import unittest

from app.incremental.lcov import parse_function_records
from app.incremental.blame import parse_porcelain
from app.incremental.git_diff import parse_unified_diff
from app.services.incremental_service import IncrementalReportService
from app.services.project_service import ProjectService
from scripts.upgrade.migration_runner import create_sqlite_schema


class CanonicalIncrementalTest(unittest.TestCase):
    def test_lcov_modern_fnl_fna_groups_by_index(self):
        ranges, fallback = parse_function_records([
            "FNL:0,100,200", "FNA:0,5,foo,with,comma"
        ])
        self.assertFalse(fallback)
        self.assertEqual(ranges, [{
            "start_line": 100, "end_line": 200,
            "name": "foo,with,comma", "format": "modern",
        }])

    def test_lcov_missing_modern_end_requires_source_fallback(self):
        ranges, fallback = parse_function_records(["FNL:0,100", "FNA:0,1,foo"])
        self.assertTrue(fallback)
        self.assertEqual(ranges[0]["end_line"], None)

    def test_real_blame_boundary_metadata(self):
        records = parse_porcelain(
            "a" * 40 + " 1 1 1\n"
            "author Alice\n"
            "author-mail <alice@example.com>\n"
            "author-time 1\n"
            "author-tz +0000\n"
            "boundary\n"
            "filename src/a.c\n"
            "\treturn 0;\n"
        )
        self.assertEqual(records[0]["author"], "Alice")
        self.assertTrue(records[0]["boundary"])
        self.assertEqual(records[0]["filename"], "src/a.c")

    def test_diff_parser_keeps_added_line_numbers(self):
        changes = parse_unified_diff(
            "diff --git a/src/a.c b/src/a.c\n"
            "+++ b/src/a.c\n"
            "@@ -1,0 +2,3 @@\n"
        )
        self.assertEqual(changes, {"src/a.c": [2, 3, 4]})

    def test_real_git_range_blame_and_lcov_ownership(self):
        from app.incremental.orchestrator import IncrementalOrchestrator

        with tempfile.TemporaryDirectory(prefix="vnext-git-") as root:
            subprocess.check_call(["git", "init", "-q", root])
            subprocess.check_call(["git", "-C", root, "config", "user.name", "Alice"])
            subprocess.check_call(["git", "-C", root, "config", "user.email", "alice@example.com"])
            source_dir = os.path.join(root, "src")
            os.makedirs(source_dir)
            source_path = os.path.join(source_dir, "a.c")
            with open(source_path, "w") as stream:
                stream.write("int main(void) {\n    return 0;\n}\n")
            subprocess.check_call(["git", "-C", root, "add", "."])
            subprocess.check_call(["git", "-C", root, "commit", "-q", "-m", "old"])
            oldgit = subprocess.check_output(
                ["git", "-C", root, "rev-parse", "HEAD"]
            ).decode().strip()
            with open(source_path, "w") as stream:
                stream.write("int main(void) {\n    int added = 1;\n    return added;\n}\n")
            subprocess.check_call(["git", "-C", root, "add", "."])
            subprocess.check_call(["git", "-C", root, "commit", "-q", "-m", "new"])
            newgit = subprocess.check_output(
                ["git", "-C", root, "rev-parse", "HEAD"]
            ).decode().strip()
            info_path = os.path.join(root, "coverage.info")
            with open(info_path, "w") as stream:
                stream.write(
                    "TN:\nSF:src/a.c\nDA:2,0\nDA:3,0\n"
                    "FNL:0,1,4\nFNA:0,1,main\nend_of_record\n"
                )
            result = IncrementalOrchestrator().build(
                "fixture", root, oldgit, newgit, info_path, repository_name="repo-a"
            )
            self.assertEqual(result["repository_name"], "repo-a")
            self.assertEqual(result["files"][0]["file_path"], "src/a.c")
            self.assertEqual(
                [item["line_number"] for item in result["files"][0]["details"]],
                [2, 3],
            )
            self.assertTrue(all(
                item["suggested_reviewer"].startswith("Alice")
                for item in result["files"][0]["details"]
            ))

    def test_vnext_incremental_resolves_build_machine_absolute_lcov_path(self):
        from app.incremental.orchestrator import IncrementalOrchestrator

        with tempfile.TemporaryDirectory(prefix="vnext-absolute-lcov-") as root:
            subprocess.check_call(["git", "init", "-q", root])
            subprocess.check_call(["git", "-C", root, "config", "user.name", "Alice"])
            subprocess.check_call([
                "git", "-C", root, "config", "user.email", "alice@example.com"
            ])
            source_dir = os.path.join(root, "src")
            os.makedirs(source_dir)
            source_path = os.path.join(source_dir, "a.c")
            with open(source_path, "w") as stream:
                stream.write("int main(void) {\n    return 0;\n}\n")
            subprocess.check_call(["git", "-C", root, "add", "."])
            subprocess.check_call(["git", "-C", root, "commit", "-q", "-m", "old"])
            oldgit = subprocess.check_output(
                ["git", "-C", root, "rev-parse", "HEAD"]
            ).decode().strip()
            with open(source_path, "w") as stream:
                stream.write("int main(void) {\n    int added = 1;\n    return added;\n}\n")
            subprocess.check_call(["git", "-C", root, "add", "."])
            subprocess.check_call(["git", "-C", root, "commit", "-q", "-m", "new"])
            newgit = subprocess.check_output(
                ["git", "-C", root, "rev-parse", "HEAD"]
            ).decode().strip()
            info_path = os.path.join(root, "coverage.info")
            with open(info_path, "w") as stream:
                stream.write(
                    "TN:\n"
                    "SF:/home/git_for_coverage/repo/src/a.c\n"
                    "DA:2,0\n"
                    "FNL:0,1,4\nFNA:0,1,main\n"
                    "end_of_record\n"
                )

            result = IncrementalOrchestrator().build(
                "fixture", root, oldgit, newgit, info_path,
                repository_name="repo-a",
            )
            self.assertEqual(result["files"][0]["file_path"], "src/a.c")
            self.assertEqual(
                [item["line_number"] for item in result["files"][0]["details"]],
                [2],
            )

    def test_vnext_incremental_rejects_ambiguous_absolute_lcov_paths(self):
        from app.incremental.orchestrator import IncrementalOrchestrator

        with tempfile.TemporaryDirectory(prefix="vnext-ambiguous-lcov-") as root:
            subprocess.check_call(["git", "init", "-q", root])
            subprocess.check_call(["git", "-C", root, "config", "user.name", "Alice"])
            subprocess.check_call([
                "git", "-C", root, "config", "user.email", "alice@example.com"
            ])
            source_dir = os.path.join(root, "src")
            os.makedirs(source_dir)
            source_path = os.path.join(source_dir, "a.c")
            with open(source_path, "w") as stream:
                stream.write("int main(void) {\n    return 0;\n}\n")
            subprocess.check_call(["git", "-C", root, "add", "."])
            subprocess.check_call(["git", "-C", root, "commit", "-q", "-m", "old"])
            oldgit = subprocess.check_output(
                ["git", "-C", root, "rev-parse", "HEAD"]
            ).decode().strip()
            with open(source_path, "w") as stream:
                stream.write("int main(void) {\n    int added = 1;\n    return added;\n}\n")
            subprocess.check_call(["git", "-C", root, "add", "."])
            subprocess.check_call(["git", "-C", root, "commit", "-q", "-m", "new"])
            newgit = subprocess.check_output(
                ["git", "-C", root, "rev-parse", "HEAD"]
            ).decode().strip()
            info_path = os.path.join(root, "coverage.info")
            with open(info_path, "w") as stream:
                stream.write(
                    "TN:\n"
                    "SF:/build/one/repo/src/a.c\nDA:2,0\nend_of_record\n"
                    "TN:\n"
                    "SF:/build/two/repo/src/a.c\nDA:2,0\nend_of_record\n"
                )

            result = IncrementalOrchestrator().build(
                "fixture", root, oldgit, newgit, info_path,
                repository_name="repo-a",
            )
            self.assertEqual(result["files"], [])

    def test_incremental_result_is_persisted_against_immutable_scan_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="vnext-incremental-db-") as root:
            subprocess.check_call(["git", "init", "-q", root])
            subprocess.check_call(["git", "-C", root, "config", "user.name", "Alice"])
            subprocess.check_call(["git", "-C", root, "config", "user.email", "alice@example.com"])
            source_dir = os.path.join(root, "src")
            os.makedirs(source_dir)
            source_path = os.path.join(source_dir, "a.c")
            with open(source_path, "w") as stream:
                stream.write("int main(void) {\n    return 0;\n}\n")
            subprocess.check_call(["git", "-C", root, "add", "."])
            subprocess.check_call(["git", "-C", root, "commit", "-q", "-m", "old"])
            oldgit = subprocess.check_output(
                ["git", "-C", root, "rev-parse", "HEAD"]
            ).decode().strip()
            with open(source_path, "w") as stream:
                stream.write("int main(void) {\n    int added = 1;\n    return added;\n}\n")
            subprocess.check_call(["git", "-C", root, "add", "."])
            subprocess.check_call(["git", "-C", root, "commit", "-q", "-m", "new"])
            newgit = subprocess.check_output(
                ["git", "-C", root, "rev-parse", "HEAD"]
            ).decode().strip()
            info_path = os.path.join(root, "coverage.info")
            with open(info_path, "w") as stream:
                stream.write(
                    "TN:\nSF:src/a.c\nDA:2,0\n"
                    "FNL:0,1,4\nFNA:0,1,main\nend_of_record\n"
                )

            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            create_sqlite_schema(connection)
            project_service = ProjectService()
            scan = project_service.create_scan(
                connection, "fixture", info_sha256="a" * 64,
                repositories=[{
                    "repository_name": "repo-a", "repository_path": root,
                    "old_commit_sha": oldgit, "new_commit_sha": newgit,
                    "verified": True,
                }],
            )
            service = IncrementalReportService(project_service.projects)
            service.allowed_roots = [root]
            result = service.build_and_persist(
                connection, "fixture", scan["id"], root, oldgit, newgit,
                info_path, repository_name="repo-a", report_id="report-a",
            )
            self.assertEqual(result["stored"]["repository_name"], "repo-a")
            loaded = service.results.load_payload(
                connection, scan["id"], "report-a", "repo-a"
            )
            self.assertEqual(loaded["scan_id"], scan["id"])
            self.assertEqual(loaded["report_id"], "report-a")
            stored_key = connection.execute(
                "SELECT incremental_key_hash FROM coverage_incremental_results "
                "WHERE scan_id=? AND report_id=? AND repository_name=?",
                (scan["id"], "report-a", "repo-a"),
            ).fetchone()[0]
            self.assertEqual(len(stored_key), 64)
            with self.assertRaises(ValueError):
                service.build_and_persist(
                    connection, "fixture", scan["id"], root, "0" * 40, newgit,
                    info_path, repository_name="repo-a", report_id="report-a",
                )
            connection.close()


if __name__ == "__main__":
    unittest.main()
