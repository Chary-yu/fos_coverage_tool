import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock
from decimal import Decimal
from datetime import datetime

from app.api.application import VNextApplication
from app.api.serialization import to_jsonable
from app.bootstrap import VNextRuntime, create_vnext_server
from app.code_detail.sidecar_store import SidecarStore
from app.code_detail.source_reader import SourceContext, SourceLineDTO, calc_sidecar_file_key
from app.code_detail.code_region import FunctionRange
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
        self.runtime.close()
        self.connection.close()

    def test_runtime_metrics_expose_cache_and_resource_counters(self):
        status, payload = self.application.dispatch("GET", "/api/coverage/metrics")
        self.assertEqual(status, 200)
        self.assertEqual(payload["runtime"], "vnext")
        self.assertIn("resources", payload["jobs"])
        self.assertIn("code_detail", payload)

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
        status, pending_page = self.application.dispatch(
            "GET", "/api/coverage/progress/pending",
            query={"project": "fixture", "page": "1", "page_size": "1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(pending_page["total"], 1)
        self.assertEqual(pending_page["rows"][0]["line_number"], 11)
        self.assertEqual(
            self.application.dispatch(
                "GET", "/api/coverage/progress", query={"project": "fixture"}
            )[1]["source"],
            "authoritative",
        )

        self.runtime.progress_service.rebuild(self.connection, "fixture", scan_id)
        progress_trace = []
        self.connection.set_trace_callback(progress_trace.append)
        summary = self.application.dispatch(
            "GET", "/api/coverage/progress", query={"project": "fixture"}
        )[1]
        self.connection.set_trace_callback(None)
        self.assertEqual(summary["source"], "coverage_file_state")
        self.assertEqual(summary["pending_total"], 1)
        self.assertFalse(
            any("SELECT * FROM coverage_file_state" in statement for statement in progress_trace)
        )
        self.assertFalse(
            any("FROM coverage_lines" in statement for statement in progress_trace)
        )
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

    def test_code_detail_binds_exact_scan_report_and_sidecar_key(self):
        with tempfile.TemporaryDirectory(prefix="vnext-report-") as report_root:
            key = calc_sidecar_file_key("src/detail.c")
            context = SourceContext(
                "detail", "src/detail.c", [
                    SourceLineDTO(1, "return 0;", coverage_state="uncovered"),
                    SourceLineDTO(2, "return 1;", coverage_state="covered"),
                ], function_ranges=[FunctionRange(1, 2, "main")],
                report_id="report_detail",
            )
            SidecarStore([report_root], chunk_size=2).save_chunked_sidecar(
                report_root, "report_detail", key, context
            )
            self.runtime.report_registry.register(
                "report_detail", [report_root], sidecar_required=True
            )
            with self.runtime.connection_context() as connection:
                scan = self.runtime.project_service.create_scan(
                    connection, "detail", info_sha256="d" * 64,
                    report={"report_id": "report_detail", "report_root": report_root},
                )
                self.runtime.project_service.ingest_files(
                    connection, scan["id"], [{
                        "file_path": "src/detail.c", "file_path_hash": "" * 0,
                        "lines": [{"line_number": 1, "coverage_state": "uncovered"}],
                    }],
                )
                layout = self.runtime.code_detail.layout(
                    connection, scan["id"], "report_detail", "src/detail.c"
                )
                self.assertEqual(layout["pending_line_count"], 1)
                lines = self.runtime.code_detail.lines(
                    connection, scan["id"], "report_detail", "src/detail.c", 1, 1
                )
                self.assertEqual(lines["lines"][0]["line_no"], 1)
                with self.assertRaises(KeyError):
                    self.runtime.code_detail.layout(
                        connection, scan["id"], "report_other", "src/detail.c"
                    )

    def test_code_detail_batch_resolves_overlay_once(self):
        with tempfile.TemporaryDirectory(prefix="vnext-batch-report-") as report_root:
            key = calc_sidecar_file_key("src/batch.c")
            context = SourceContext(
                "batch", "src/batch.c", [
                    SourceLineDTO(1, "one();", coverage_state="uncovered"),
                    SourceLineDTO(2, "two();", coverage_state="uncovered"),
                    SourceLineDTO(3, "three();", coverage_state="covered"),
                    SourceLineDTO(4, "four();", coverage_state="covered"),
                ], report_id="report_batch",
            )
            SidecarStore([report_root], chunk_size=2).save_chunked_sidecar(
                report_root, "report_batch", key, context
            )
            self.runtime.report_registry.register(
                "report_batch", [report_root], sidecar_required=True
            )
            with self.runtime.connection_context() as connection:
                scan = self.runtime.project_service.create_scan(
                    connection, "batch", info_sha256="b" * 64,
                    report={"report_id": "report_batch", "report_root": report_root},
                )
                self.runtime.project_service.ingest_files(
                    connection, scan["id"], [{
                        "file_path": "src/batch.c", "file_path_hash": "" * 0,
                        "lines": [
                            {"line_number": 1, "coverage_state": "uncovered"},
                            {"line_number": 2, "coverage_state": "uncovered"},
                            {"line_number": 3, "coverage_state": "covered"},
                            {"line_number": 4, "coverage_state": "covered"},
                        ],
                    }],
                )
                trace = []
                connection.set_trace_callback(trace.append)
                batches = self.runtime.code_detail.lines_batch(
                    connection, scan["id"], "report_batch", "src/batch.c",
                    [(1, 1), (2, 2), (3, 4)],
                )
                # A second HTTP-equivalent request for the same immutable
                # data_version reuses the overlay instead of rereading the
                # same analysis rows.
                self.runtime.code_detail.lines_batch(
                    connection, scan["id"], "report_batch", "src/batch.c",
                    [(1, 1), (2, 2), (3, 4)],
                )
                connection.set_trace_callback(None)

            self.assertEqual([len(item["lines"]) for item in batches], [1, 1, 2])
            analysis_reads = [
                statement for statement in trace
                if "FROM coverage_analyses" in statement
            ]
            self.assertEqual(
                len(analysis_reads), 1,
                "batch code detail should load the file overlay once",
            )

    def test_sidecar_shared_physical_chunk_is_decoded_once(self):
        with tempfile.TemporaryDirectory(prefix="vnext-sidecar-cache-") as root:
            store = SidecarStore([root], chunk_size=4)
            context = SourceContext(
                "cache", "src/cache.c", [
                    SourceLineDTO(i, "line{}".format(i), coverage_state="covered")
                    for i in range(1, 9)
                ], report_id="report_cache",
            )
            key = calc_sidecar_file_key("src/cache.c")
            store.save_chunked_sidecar(root, "report_cache", key, context)
            with mock.patch("app.code_detail.sidecar_store.json.load", wraps=json.load) as load:
                first = store.load_lines_ranges(
                    "report_cache", key, [(1, 2), (3, 4)]
                )
                second = store.load_lines_ranges(
                    "report_cache", key, [(1, 2), (3, 4)]
                )
                self.assertEqual(load.call_count, 2, "meta + one physical chunk")
            stats = store.cache_stats()
            self.assertEqual(stats["metadata_reads"], 1)
            self.assertGreaterEqual(stats["metadata_cache_hits"], 1)
            self.assertEqual(stats["chunk_reads"], 1)
            self.assertGreaterEqual(stats["chunk_cache_hits"], 1)
            self.assertEqual(stats["path_resolution_reads"], 1)
        self.assertEqual([[line["line_no"] for line in rows] for rows in first], [[1, 2], [3, 4]])
        self.assertEqual([[line["line_no"] for line in rows] for rows in second], [[1, 2], [3, 4]])


if __name__ == "__main__":
    unittest.main()
