import json
import os
import sqlite3
import tempfile
import unittest
import zipfile

from app.bootstrap import VNextRuntime
from app.code_detail.sidecar_store import SidecarStore
from app.services.analysis_service import AnalysisService
from app.services.export_service import ExportService
from app.services.project_service import ProjectService
from scripts.upgrade.migration_runner import create_sqlite_schema


class VNextApiExportSecurityTest(unittest.TestCase):
    def setUp(self):
        self.state_root = tempfile.mkdtemp(prefix="vnext-api-state-")
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        create_sqlite_schema(self.connection)

    def tearDown(self):
        runtime = getattr(self, "runtime", None)
        if runtime is not None:
            runtime.close()
        self.connection.close()

    def _runtime(self, auth=None, server=None):
        config = {
            "project_name": "fixture",
            "auth": auth or {"mode": "disabled"},
            "runtime_state": {"root": self.state_root},
        }
        if server is not None:
            config["server"] = server
        self.runtime = VNextRuntime(config, os.getcwd(), connection=self.connection)
        return self.runtime

    def test_mutation_auth_origin_and_write_freeze_are_uniform(self):
        runtime = self._runtime({
            "mode": "reverse_proxy",
            "trusted_proxy_addresses": ["10.0.0.5"],
            "user_header": "X-Remote-User",
            "allowed_origins": ["https://candidate.example"],
        })
        app = runtime.application()
        status, _ = app.dispatch(
            "POST", "/api/coverage/projects", body={"project_name": "fixture"},
            remote_address="10.0.0.5",
        )
        self.assertEqual(status, 401)
        status, _ = app.dispatch(
            "POST", "/api/coverage/projects", body={"project_name": "fixture"},
            headers={"X-Remote-User": "alice"}, remote_address="10.0.0.6",
        )
        self.assertEqual(status, 401)
        status, _ = app.dispatch(
            "POST", "/api/coverage/projects", body={"project_name": "fixture"},
            headers={"X-Remote-User": "alice", "Origin": "https://evil.example"},
            remote_address="10.0.0.5",
        )
        self.assertEqual(status, 403)
        status, _ = app.dispatch(
            "POST", "/api/coverage/projects", body={"project_name": "fixture"},
            headers={"X-Remote-User": "alice", "Origin": "https://candidate.example"},
            remote_address="10.0.0.5",
        )
        self.assertEqual(status, 201)

        marker = os.path.join(self.state_root, "upgrade-writes-frozen.json")
        with open(marker, "w") as stream:
            stream.write("{}")
        status, payload = app.dispatch(
            "POST", "/api/coverage/projects", body={"project_name": "blocked"},
            headers={"X-Remote-User": "alice", "Origin": "https://candidate.example"},
            remote_address="10.0.0.5",
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "forbidden")
        status, payload = app.dispatch("GET", "/api/coverage/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["runtime"], "vnext")

    def test_authenticated_operator_reads_survive_write_freeze(self):
        runtime = self._runtime({
            "mode": "reverse_proxy",
            "trusted_proxy_addresses": ["10.0.0.5"],
            "user_header": "X-Remote-User",
        })
        app = runtime.application()
        marker = os.path.join(self.state_root, "upgrade-writes-frozen.json")
        with open(marker, "w") as stream:
            stream.write("{}")
        headers = {"X-Remote-User": "operator"}
        status, payload = app.dispatch(
            "GET", "/api/coverage/routes", headers=headers,
            remote_address="10.0.0.5",
        )
        self.assertEqual(status, 200)
        self.assertIn("routes", payload)
        status, _ = app.dispatch(
            "POST", "/api/coverage/projects", body={"project_name": "blocked"},
            headers=headers, remote_address="10.0.0.5",
        )
        self.assertEqual(status, 503)

    def test_non_loopback_bind_protects_data_reads(self):
        runtime = self._runtime({
            "mode": "reverse_proxy",
            "trusted_proxy_addresses": ["10.0.0.5"],
            "user_header": "X-Remote-User",
        }, server={"host": "0.0.0.0", "port": 9528})
        app = runtime.application()
        status, _ = app.dispatch("GET", "/api/coverage/projects", remote_address="10.0.0.5")
        self.assertEqual(status, 401)
        status, _ = app.dispatch(
            "GET", "/api/coverage/projects",
            headers={"X-Remote-User": "operator"}, remote_address="10.0.0.5",
        )
        self.assertEqual(status, 200)

    def test_malformed_identity_and_batch_limits_fail_closed(self):
        app = self._runtime().application()
        status, _ = app.dispatch("GET", "/api/coverage/code-layout", query={})
        self.assertEqual(status, 400)
        status, _ = app.dispatch(
            "GET", "/api/coverage/code-layout",
            query={"scan_id": 1, "report_id": "report", "file_path": "../escape.c"},
        )
        self.assertEqual(status, 400)
        status, _ = app.dispatch(
            "POST", "/api/coverage/code-lines/batch",
            body={"scan_id": 1, "report_id": "report", "file_path": "x.c",
                  "ranges": [{}] * 1001},
        )
        self.assertEqual(status, 400)
        status, _ = app.dispatch("GET", "/api/coverage/not-a-route")
        self.assertEqual(status, 404)

    def test_export_preserves_scan_identity_and_reviewer_semantics(self):
        project_repo = ProjectService()
        scan = project_repo.create_scan_and_ingest(
            self.connection, "export-project", [{
                "repository_name": "repo-a",
                "file_path": "src/a.c",
                "file_path_hash": "a" * 32,
                "lines": [{
                    "line_number": 10, "line_text": "return 0;",
                    "coverage_state": "uncovered", "suggested_reviewer": "git-alice",
                }],
            }],
            info_sha256="b" * 64,
            repositories=[{
                "repository_name": "repo-a", "repository_path": "/candidate/repo-a",
                "old_commit_sha": "1" * 40, "new_commit_sha": "2" * 40,
                "verified": True,
            }],
            report={"report_id": "export-report"},
        )
        line_id = self.connection.execute("SELECT id FROM coverage_lines").fetchone()[0]
        AnalysisService().save(
            self.connection, "export-project", scan["id"],
            [{"line_id": line_id, "status": "可覆盖", "reviewer": "db-carol",
              "coverage_method": "unit"}],
        )
        with tempfile.TemporaryDirectory(prefix="vnext-export-") as output_root:
            target = ExportService(
                project_repo.projects, output_root,
                release_identity={"build_id": "candidate-build"},
            ).export_scan(
                self.connection, "export-project", scan["id"], "export-report"
            )
            self.assertTrue(os.path.isfile(target))
            with zipfile.ZipFile(target) as archive:
                metadata = json.loads(archive.read("metadata.json").decode("utf-8"))
                rows = [json.loads(line) for line in archive.read(
                    "coverage_lines.jsonl").decode("utf-8").splitlines()]
            self.assertEqual(metadata["scan_id"], scan["id"])
            self.assertEqual(metadata["report_id"], "export-report")
            self.assertEqual(metadata["release"]["build_id"], "candidate-build")
            self.assertEqual(rows[0]["suggested_reviewer"], "git-alice")
            self.assertEqual(rows[0]["reviewer"], "db-carol")
            with self.assertRaises(ValueError):
                ExportService(project_repo.projects, output_root).export_scan(
                    self.connection, "export-project", scan["id"],
                    output_path=os.path.join(output_root, "..", "escape.zip"),
                )
                with self.assertRaises(KeyError):
                    ExportService(project_repo.projects, output_root).export_scan(
                        self.connection, "export-project", scan["id"], "wrong-report",
                    )

    def test_analysis_save_resolves_and_upserts_records_in_bulk(self):
        project_repo = ProjectService()
        scan = project_repo.create_scan_and_ingest(
            self.connection, "bulk-analysis", [{
                "repository_name": "repo-a",
                "file_path": "src/bulk.c",
                "file_path_hash": "b" * 32,
                "lines": [
                    {"line_number": 10, "coverage_state": "uncovered"},
                    {"line_number": 11, "coverage_state": "uncovered"},
                    {"line_number": 12, "coverage_state": "uncovered"},
                ],
            }], info_sha256="c" * 64,
        )
        records = [{
            "repository_name": "repo-a", "file_path_hash": "b" * 32,
            "line_number": line_number, "status": "可覆盖",
        } for line_number in (10, 11, 12)]
        trace = []
        self.connection.set_trace_callback(trace.append)
        result = AnalysisService().save(
            self.connection, "bulk-analysis", scan["id"], records, reviewer="alice"
        )
        self.connection.set_trace_callback(None)

        self.assertEqual(result["saved"], 3)
        self.assertFalse(
            any("SELECT * FROM coverage_analyses WHERE line_id" in statement
                for statement in trace),
            "bulk save must not read each analysis row individually",
        )
        self.assertEqual(
            len([statement for statement in trace if "FROM coverage_analyses a" in statement]),
            1,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM coverage_analyses").fetchone()[0],
            3,
        )

    def test_sidecar_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="vnext-sidecar-") as root:
            outside = os.path.join(root, "outside")
            os.makedirs(outside)
            cache = os.path.join(root, ".source_cache")
            os.makedirs(cache)
            try:
                os.symlink(outside, os.path.join(cache, "report-a"))
            except (AttributeError, OSError):
                self.skipTest("symlink creation is unavailable")
            store = SidecarStore([root])
            self.assertIsNone(store.load_metadata("report-a", "file-key"))
            self.assertIsNone(store.load_lines_range("report-a", "file-key", 1, 2))


if __name__ == "__main__":
    unittest.main()
