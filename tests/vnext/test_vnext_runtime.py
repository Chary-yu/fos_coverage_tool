import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from decimal import Decimal
from datetime import datetime

from app.api.application import VNextApplication
from app.api.serialization import to_jsonable
from app.bootstrap import VNextRuntime, create_vnext_server
from scripts.upgrade.migration_runner import create_sqlite_schema


class VNextRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        create_sqlite_schema(self.connection)
        config = {
            "project_name": "fixture",
            "auth": {"mode": "disabled"},
            "runtime_state": {"root": tempfile.mkdtemp(prefix="vnext-state-")},
        }
        self.runtime = VNextRuntime(config, os.getcwd(), connection=self.connection)
        self.application = self.runtime.application()

    def tearDown(self):
        self.connection.close()

    def test_scan_analysis_freshness_and_report_identity(self):
        status, body = self.application.dispatch(
            "POST", "/api/coverage/projects", body={"project_name": "fixture"}
        )
        self.assertEqual(status, 201)
        status, body = self.application.dispatch(
            "POST", "/api/coverage/scans",
            body={
                "project_name": "fixture",
                "info_sha256": "a" * 64,
                "report": {"report_id": "report_fixture"},
                "repositories": [{
                    "repository_name": "repo-a", "repository_path": "/candidate/repo-a",
                    "branch_name": "main", "old_commit_sha": "1" * 40,
                    "new_commit_sha": "2" * 40, "verified": True,
                }],
            }
        )
        self.assertEqual(status, 201)
        scan_id = body["scan"]["id"]
        self.runtime.project_service.ingest_files(
            self.connection, scan_id, [{
                "repository_name": "repo-a",
                "file_path": "src/fixture.c",
                "file_path_hash": "f" * 32,
                "lines": [
                    {"line_number": 10, "line_text": "fixture();",
                     "coverage_state": "uncovered", "block_start_line": 10,
                     "block_end_line": 10, "code_line_hash": "l10"},
                    {"line_number": 11, "line_text": "fixture2();",
                     "coverage_state": "uncovered", "block_start_line": 11,
                     "block_end_line": 11, "code_line_hash": "l11"},
                ],
            }]
        )
        status, pending = self.application.dispatch(
            "GET", "/api/coverage/incremental/unanalyzed",
            query={"project": "fixture"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(pending["files"][0]["pending_line_numbers"], [10, 11])

        line_rows = self.connection.execute(
            "SELECT id FROM coverage_lines ORDER BY line_number"
        ).fetchall()
        status, saved = self.application.dispatch(
            "POST", "/api/coverage/analysis",
            body={
                "project_name": "fixture", "scan_id": scan_id, "reviewer": "alice",
                "records": [{"line_id": line_rows[0][0], "status": "可覆盖",
                             "coverage_method": "unit"}],
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved["saved"], 1)
        status, pending = self.application.dispatch(
            "GET", "/api/coverage/incremental/unanalyzed",
            query={"project": "fixture"},
        )
        self.assertEqual(pending["files"][0]["pending_line_numbers"], [11])
        self.assertEqual(
            self.application.dispatch(
                "GET", "/api/coverage/progress", query={"project": "fixture"}
            )[1]["source"],
            "authoritative",
        )

        self.runtime.progress_service.rebuild(self.connection, "fixture", scan_id)
        summary = self.application.dispatch(
            "GET", "/api/coverage/progress", query={"project": "fixture"}
        )[1]
        self.assertEqual(summary["source"], "coverage_file_state")
        self.assertEqual(summary["pending_total"], 1)
        report = self.connection.execute(
            "SELECT scan_id FROM coverage_reports WHERE report_id = ?",
            ("report_fixture",),
        ).fetchone()
        self.assertEqual(report[0], scan_id)

    def test_serializer_handles_cross_layer_values(self):
        value = to_jsonable({
            "amount": Decimal("1.50"),
            "when": datetime(2026, 8, 20, 1, 2, 3),
            "items": {"a", "b"},
        })
        self.assertEqual(value["amount"], 1.5)
        self.assertEqual(value["when"], "2026-08-20T01:02:03")
        self.assertEqual(sorted(value["items"]), ["a", "b"])

    def test_info_import_creates_immutable_scan_and_function_fallback(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".info", delete=False) as stream:
            stream.write(
                "TN:\n"
                "SF:src/imported.c\n"
                "DA:10,0\n"
                "DA:11,1\n"
                "FNL:0,10\n"
                "FNA:0,1,main\n"
                "end_of_record\n"
            )
            info_path = stream.name
        try:
            with self.runtime.connection_context() as connection:
                result = self.runtime.scan_import_service.import_info(
                    connection, "imported", info_path
                )
            self.assertEqual(result["files"], 1)
            self.assertEqual(result["line_count"], 2)
            self.assertEqual(result["function_range_fallback_files"], 1)
            scan_count = self.connection.execute(
                "SELECT COUNT(*) FROM coverage_scans WHERE project_id = "
                "(SELECT id FROM coverage_projects WHERE project_name = 'imported')"
            ).fetchone()[0]
            self.assertEqual(scan_count, 1)
        finally:
            os.remove(info_path)

    def test_real_stdlib_http_transport_uses_one_api_base(self):
        config = {
            "project_name": "fixture",
            "auth": {"mode": "disabled"},
            "runtime_state": {"root": tempfile.mkdtemp(prefix="vnext-http-")},
        }
        server = create_vnext_server(
            ("127.0.0.1", 0), config, repo_root=os.getcwd(),
            connection=self.connection,
        )
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        try:
            url = "http://127.0.0.1:{}/api/coverage/health".format(
                server.server_address[1]
            )
            with urllib.request.urlopen(url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["runtime"], "vnext")
            self.assertEqual(payload["schema_version"], 1)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
